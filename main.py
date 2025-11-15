import socket
import json
import sys
import google.generativeai as genai2
from google.genai import types
from google import genai
from instructions import first_agent, describer_agent, coding_agent, reviewer_agent, effects_agent
from utils import convert_text_to_dict, parse_enhancements_to_dict
from dotenv import load_dotenv
from math import radians
import datetime
import os
import re

# Load environment variables (e.g., GEMINI_API_KEY)
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai2.configure(api_key=GEMINI_API_KEY)

# Initialize the Generative AI agents
agent = genai2.GenerativeModel(
    model_name="gemini-2.0-flash",
    system_instruction=first_agent,
)

describer = genai2.GenerativeModel(
    model_name="gemini-2.0-flash",
    system_instruction=describer_agent
)

def take_multiple_screenshots(output_dir="screenshots", angles=None, resolution=(1920, 1080)):
    """
    Takes multiple screenshots of the current Blender scene from various angles.

    Args:
        output_dir (str): Directory where screenshots will be saved.
        angles (list[tuple[str, tuple[float, float, float]]]): 
            List of (angle_name, rotation_euler) tuples in radians.
            Defaults to a set of standard views: front, side, top, perspective.
        resolution (tuple[int, int]): Resolution (x, y) for screenshots.

    Returns:
        dict: Blender MCP response for the final screenshot command.
    """
    if angles is None:
        # Default camera angles: (name, (rot_x, rot_y, rot_z)) in radians
        from math import radians
        angles = [
            ("front", (radians(90), 0, radians(0))),
            ("side",  (radians(90), 0, radians(90))),
            ("top",   (radians(0), 0, radians(0))),
            ("perspective", (radians(60), 0, radians(45))),
        ]

    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    # Construct Blender Python script
    script_lines = [
        "import bpy, os, math",
        f"os.makedirs(r'{output_dir}', exist_ok=True)",
        "scene = bpy.context.scene",
        "cameras = [obj for obj in bpy.data.objects if obj.type == 'CAMERA']",
        "if not cameras:",
        "    bpy.ops.object.camera_add(location=(0, -10, 5))",
        "    camera = bpy.context.object",
        "    scene.camera = camera",
        "else:",
        "    camera = cameras[0]",
        f"scene.render.image_settings.file_format = 'PNG'",
        f"scene.render.resolution_x = {resolution[0]}",
        f"scene.render.resolution_y = {resolution[1]}",
        "scene.render.resolution_percentage = 100",
    ]

    for name, (rx, ry, rz) in angles:
        file_path = os.path.join(output_dir, f"screenshot_{name}_{timestamp}.png")
        script_lines.append(f"\n# --- {name.upper()} VIEW ---")
        script_lines.append(f"camera.rotation_euler = ({rx}, {ry}, {rz})")
        script_lines.append(f"scene.render.filepath = r'{file_path}'")
        script_lines.append("bpy.ops.render.render(write_still=True)")
        script_lines.append(f"print('Saved:', r'{file_path}')")

    screenshot_script = "\n".join(script_lines)

    print("\n--- Sending multi-angle screenshot command to Blender ---")
    response = send_to_blender(screenshot_script)

    if response and response.get("status") == "success":
        print(f"✅ All screenshots saved successfully in '{output_dir}/'")
    else:
        print("❌ Failed to take screenshots.")
        print("Response:", response)

    return response

def get_scene_context():
    """
    Get current Blender scene context by executing a script in Blender.
    Returns the scene context data for use in AI prompts.
    """
    context_script = '''
import bpy
import json
from mathutils import Vector

def get_scene_summary():
    """Get a concise summary of the current scene for including in AI prompts."""
    scene = bpy.context.scene
    objects = list(bpy.context.view_layer.objects)
    
    summary = f"Current Blender Scene Context:\\n"
    summary += f"Scene Name: {scene.name}\\n"
    summary += f"Total Objects: {len(objects)}\\n\\n"
    
    # Group objects by type
    object_types = {}
    for obj in objects:
        if obj.type not in object_types:
            object_types[obj.type] = []
        object_types[obj.type].append(obj)
    
    summary += "Objects by Type:\\n"
    for obj_type, objs in object_types.items():
        summary += f"- {obj_type}: {len(objs)} objects\\n"
        for obj in objs[:5]:  # Show first 5 objects of each type
            location_str = f"({obj.location.x:.2f}, {obj.location.y:.2f}, {obj.location.z:.2f})"
            summary += f"  * {obj.name} at {location_str}"
            if obj.scale != Vector((1.0, 1.0, 1.0)):
                scale_str = f"({obj.scale.x:.2f}, {obj.scale.y:.2f}, {obj.scale.z:.2f})"
                summary += f", scale {scale_str}"
            if obj.material_slots:
                materials = [slot.material.name for slot in obj.material_slots if slot.material]
                if materials:
                    summary += f", materials: {', '.join(materials)}"
            summary += "\\n"
        if len(objs) > 5:
            summary += f"  * ... and {len(objs) - 5} more\\n"
    
    # Show materials
    materials = list(bpy.data.materials)
    if materials:
        summary += f"\\nMaterials ({len(materials)}): "
        material_names = [mat.name for mat in materials[:5]]
        summary += ", ".join(material_names)
        if len(materials) > 5:
            summary += f" and {len(materials) - 5} more"
        summary += "\\n"
    
    # Show active object
    if bpy.context.active_object:
        active = bpy.context.active_object
        location_str = f"({active.location.x:.2f}, {active.location.y:.2f}, {active.location.z:.2f})"
        summary += f"\\nActive Object: {active.name} ({active.type}) at {location_str}\\n"
    
    # Show world/lighting info
    if scene.world:
        summary += f"\\nWorld: {scene.world.name}\\n"
    
    # Show camera info
    cameras = [obj for obj in objects if obj.type == 'CAMERA']
    if cameras:
        cam = cameras[0]  # Use first camera
        location_str = f"({cam.location.x:.2f}, {cam.location.y:.2f}, {cam.location.z:.2f})"
        summary += f"Camera: {cam.name} at {location_str}\\n"
    
    return summary

def export_detailed_scene_context():
    """Export detailed scene context to JSON format."""
    context_data = {
        "scene_name": bpy.context.scene.name,
        "objects": [],
        "materials": [],
        "scene_properties": {}
    }
    
    # Export scene properties
    scene = bpy.context.scene
    context_data["scene_properties"] = {
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
        "frame_current": scene.frame_current,
        "world_name": scene.world.name if scene.world else None,
        "render_engine": scene.render.engine
    }
    
    # Export all objects in the scene
    for obj in bpy.context.view_layer.objects:
        obj_data = {
            "name": obj.name,
            "type": obj.type,
            "location": [round(obj.location.x, 3), round(obj.location.y, 3), round(obj.location.z, 3)],
            "rotation_euler": [round(obj.rotation_euler.x, 3), round(obj.rotation_euler.y, 3), round(obj.rotation_euler.z, 3)],
            "scale": [round(obj.scale.x, 3), round(obj.scale.y, 3), round(obj.scale.z, 3)],
            "dimensions": [round(obj.dimensions.x, 3), round(obj.dimensions.y, 3), round(obj.dimensions.z, 3)],
            "visible": obj.visible_get(),
            "parent": obj.parent.name if obj.parent else None,
            "children": [child.name for child in obj.children],
            "material_slots": []
        }
        
        # Export material slots
        for slot in obj.material_slots:
            if slot.material:
                obj_data["material_slots"].append(slot.material.name)
        
        # Type-specific data
        if obj.type == "MESH" and obj.data:
            obj_data["mesh_data"] = {
                "vertices_count": len(obj.data.vertices),
                "faces_count": len(obj.data.polygons)
            }
        elif obj.type == "LIGHT" and obj.data:
            obj_data["light_data"] = {
                "light_type": obj.data.type,
                "energy": obj.data.energy,
                "color": [obj.data.color.r, obj.data.color.g, obj.data.color.b]
            }
        elif obj.type == "CAMERA" and obj.data:
            obj_data["camera_data"] = {
                "lens": obj.data.lens,
                "clip_start": obj.data.clip_start,
                "clip_end": obj.data.clip_end
            }
        
        context_data["objects"].append(obj_data)
    
    # Export materials
    for material in bpy.data.materials:
        mat_data = {
            "name": material.name,
            "use_nodes": material.use_nodes,
            "users": material.users
        }
        if hasattr(material, 'diffuse_color'):
            mat_data["diffuse_color"] = [material.diffuse_color.r, material.diffuse_color.g, material.diffuse_color.b, material.diffuse_color.a]
        context_data["materials"].append(mat_data)
    
    return json.dumps(context_data, indent=2)

# Execute and return both summary and detailed context
try:
    summary = get_scene_summary()
    detailed_context = export_detailed_scene_context()
    
    # Save detailed context to file
    with open("current_scene_context.json", "w") as f:
        f.write(detailed_context)
    
    print("SCENE_CONTEXT_START")
    print(summary)
    print("SCENE_CONTEXT_END")
    print("DETAILED_CONTEXT_SAVED")
except Exception as e:
    print(f"Error getting scene context: {str(e)}")
    print("SCENE_CONTEXT_START")
    print("Empty scene or error occurred")
    print("SCENE_CONTEXT_END")
'''
    
    # Send context script to Blender and get response
    response = send_to_blender(context_script)
    
    scene_context = "Empty scene"
    if response and response.get("status") == "success":
        if isinstance(response.get('result'), dict):
            stdout = response['result'].get('stdout', '')
        else:
            stdout = response.get('stdout', '')
        
        # Extract scene context from stdout
        if "SCENE_CONTEXT_START" in stdout and "SCENE_CONTEXT_END" in stdout:
            start_idx = stdout.find("SCENE_CONTEXT_START") + len("SCENE_CONTEXT_START")
            end_idx = stdout.find("SCENE_CONTEXT_END")
            scene_context = stdout[start_idx:end_idx].strip()
    
    return scene_context

def generate(coding_agent, prompt, scene_context=""):
    client = genai.Client(
        api_key=os.environ.get("GEMINI_API_KEY"),
    )

    model = "gemini-2.5-pro"
    uploaded_file1 = client.files.upload(file="scraping_results/results_20250911_102323.txt")
    uploaded_file2 = client.files.upload(file="scraping_results/results_new.txt")
    
    # Enhanced prompt with scene context and Chain of Thought reasoning
    cot_prompt = f"""Please think through this problem step by step before providing the final Blender Python code.

        CURRENT SCENE CONTEXT:
        {scene_context}

        REASONING PROCESS:
        1. Analyze the current scene and existing objects
        2. Understand what new elements need to be added based on the description
        3. Consider positioning relative to existing objects to avoid overlaps
        4. Plan appropriate materials and lighting that complement existing scene
        5. Consider Blender API methods and compatibility
        6. Plan code structure and execution order
        7. Optimize for performance and reliability

        IMPORTANT GUIDELINES:
        - Position new objects thoughtfully relative to existing objects
        - Avoid placing objects at the same location as existing ones
        - Consider the scale and dimensions of existing objects
        - Use appropriate materials that fit the scene's style
        - Ensure new objects are visible and properly lit

        After your reasoning, provide the final Blender Python code.

        TASK:
        {prompt}"""

    parts = [
        types.Part.from_text(text=cot_prompt),
        types.Part(file_data=types.FileData(file_uri=uploaded_file1.uri)),
        types.Part(file_data=types.FileData(file_uri=uploaded_file2.uri)),
    ]

    contents = [
        types.Content(
            role="user",
            parts=parts,
        ),
    ]
    
    tools = [
        types.Tool(google_search=types.GoogleSearch()),  # Enable internet access
    ]

    generate_content_config = types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(
            thinking_budget=10000,  # Enable extended reasoning
        ),
        tools=tools,
        system_instruction=[
            types.Part.from_text(text=coding_agent),
        ],
    )

    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=generate_content_config,
    )

    # Extract and log the response
    full_response = response.text
    
    # Log the full response for debugging (may contain CoT reasoning)
    print("=== MODEL RESPONSE ===")
    print(full_response)
    print("=== END MODEL RESPONSE ===")
    
    return full_response

def review_scene_with_images(reviewer_agent, scene_description, screenshot_paths, feedback_focus="visual accuracy and completeness"):
    
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    model = "gemini-2.5-pro"

    # Upload screenshots
    uploaded_files = []
    for img_path in screenshot_paths:
        if os.path.exists(img_path):
            uploaded = client.files.upload(file=img_path)
            uploaded_files.append(uploaded)
        else:
            print(f"⚠️ Warning: Screenshot not found: {img_path}")

    review_prompt = f"""
        TASK:
        You are provided with one or more screenshots of a Blender-rendered scene and the textual description
        that the scene was intended to represent.

        Your job is to critically assess whether the screenshots accurately reflect the description.

        Focus areas:
        - Visual and structural accuracy (are all described elements present?)
        - Lighting, materials, and colors compared to the text.
        - Scale, proportion, and spatial arrangement of key objects.
        - Consistency with described mood, atmosphere, or theme.
        - Identify any mismatches, omissions, or improvements needed.

        Be objective, descriptive, and detailed in your feedback.
        Conclude with a final rating (e.g., 8/10) for how accurately the renders match the description.

        DESCRIPTION:
        {scene_description}

        FEEDBACK FOCUS: {feedback_focus}
        """

    # Build parts (text + image files)
    parts = [types.Part.from_text(text=review_prompt)]
    for uploaded in uploaded_files:
        parts.append(types.Part(file_data=types.FileData(file_uri=uploaded.uri)))

    # Define the conversation
    contents = [
        types.Content(role="user", parts=parts)
    ]

    # Optionally enable external search tools
    tools = [types.Tool(google_search=types.GoogleSearch())]

    # Define configuration
    review_config = types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_budget=8000),
        tools=tools,
        system_instruction=[
            types.Part.from_text(text=reviewer_agent)
        ]
    )

    print("\n--- Sending multimodal review request to Gemini ---")
    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=review_config,
    )

    review_text = response.text

    print("=== GEMINI REVIEW RESPONSE ===")
    print(review_text)
    print("=== END REVIEW ===")

    return review_text

def send_to_blender(script_code):
    """
    Connects to the Blender MCP server, sends a script, and returns the response,
    capturing all of stdout and stderr/log output.
    """
    HOST = 'localhost'
    PORT = 8000
    command = {
        "type": "execute_code",
        "params": {"code": script_code}
    }
    message = json.dumps(command)
    print(f"Connecting to Blender on {HOST}:{PORT}...")
    try:
        # Create a socket client
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            # Connect to the Blender server
            s.connect((HOST, PORT))
            print("Connection successful.")

            # Send the JSON message
            # It needs to be encoded into bytes
            s.sendall(message.encode('utf-8'))
            print("Sent command to Blender.")

            # Wait for a response from Blender
            response = s.recv(8192)
            response_data = json.loads(response.decode('utf-8'))

            # Print the response
            print("\n--- Response from Blender ---")
            print(json.dumps(response_data, indent=2))
            print("---------------------------\n")
            return response_data

    except ConnectionRefusedError:
        print("\nError: Connection refused. Please ensure the Blender MCP server is running.")
        sys.exit(1)
    except Exception as e:
        print(f"\nAn unexpected error occurred: {e}")
        return {"status": "error", "message": str(e)}

def extract_sections_by_roman_numerals(text):
    """
    Extracts content under each Roman numeral section from the input text.
    Handles format: **I. Section Name:**
    
    Args:
        text (str): Input text with Roman numeral sections
        
    Returns:
        dict: Dictionary with Roman numeral sections as keys and content as values
    """
    # Pattern to match **I. Section Name:** format
    pattern = r'\*\*([IVXLCDM]+)\.\s*([^*]+)\*\*'
    
    # Find all section headers
    sections = {}
    current_section = None
    current_content = []
    
    lines = text.split('\n')
    
    for line in lines:
        line = line.strip()
        
        # Check if this line is a section header
        match = re.match(pattern, line)
        if match:
            # If we were already processing a section, save it
            if current_section and current_content:
                sections[current_section] = '\n'.join(current_content).strip()
            
            # Start new section
            roman_num = match.group(1)
            section_name = match.group(2).strip()
            current_section = f"{roman_num}. {section_name}"
            current_content = []
        elif current_section:
            # Add content to current section (skip empty lines at beginning)
            if line or current_content:  # Allow empty lines only if we already have content
                current_content.append(line)
    
    # Add the last section
    if current_section and current_content:
        sections[current_section] = '\n'.join(current_content).strip()
    
    return sections

def get_error_details(blender_response):
    """Extract error details from blender response for logging."""
    if not blender_response:
        return "No response received."
    
    if isinstance(blender_response.get('result'), dict):
        stderr = blender_response['result'].get('stderr', '')
        message = blender_response.get('message', '')
        return f"STDERR: {stderr}, MESSAGE: {message}"
    else:
        stderr = blender_response.get('stderr', '')
        message = blender_response.get('message', '')
        return f"STDERR: {stderr}, MESSAGE: {message}"

def log_final_error(blender_response):
    """Log final error details when all retries are exhausted."""
    if blender_response:
        if isinstance(blender_response.get('result'), dict):
            print(f"Final failed attempt details:\nSTDOUT:\n{blender_response['result'].get('stdout','')}\nSTDERR:\n{blender_response['result'].get('stderr','')}\nMESSAGE:\n{blender_response.get('message','')}")
        else:
            print(f"Final failed attempt details:\nSTDOUT:\n{blender_response.get('stdout','')}\nSTDERR:\n{blender_response.get('stderr','')}\nMESSAGE:\n{blender_response.get('message','')}")
    else:
        print("Final failed attempt: No response received.")

def extract_key_elements_section(text):
    """
    Extract the "Key Elements and Details" section from the text.
    
    Args:
        text (str): The full text containing the scene description
        
    Returns:
        str: The extracted "Key Elements and Details" section, or None if not found
    """
    pattern = r"Key Elements and Details\s*\n(.*?)(?=\n\s*-+\s*\n|\n\s*[A-Z][a-zA-Z\s]+[A-Z][a-zA-Z\s-]*:|\n\s*[A-Z][a-zA-Z\s]+[A-Z][a-zA-Z\s-]*\s*\n|\Z)"
    
    match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    
    if match:
        return match.group(1).strip()
    return None

def get_all_elements(section_text):
    """
    Extract all elements from the Key Elements and Details section.
    
    Args:
        section_text (str): The extracted "Key Elements and Details" section
        
    Returns:
        list: List of all elements in the format (category, element_name, description)
    """
    elements = []
    
    # Find all elements in the format: (Element Name): Description
    element_pattern = r'\(\s*([^)]+)\s*\):\s*(.*?)(?=\n\s*\(|\n\s*-|\Z)'
    matches = re.finditer(element_pattern, section_text, re.DOTALL)
    
    for match in matches:
        element_name = match.group(1).strip()
        description = match.group(2).strip()
        
        # Find which category this element belongs to by looking backwards
        # for the nearest main category (line starting with "- CategoryName")
        lines_before = section_text[:match.start()].split('\n')
        category = "Unknown"
        
        for line in reversed(lines_before):
            if re.match(r'\s*-\s*[A-Z][a-zA-Z\s]*[A-Za-z]\s*$', line.strip()):
                category = re.sub(r'^\s*-\s*', '', line.strip())
                break
        
        elements.append((category, element_name, description))
    
    return elements

def extract_final_enhancements_section(text):
    """
    Extract the "Final specific enhancements and details to enrich the scene" section from the text.
    
    Args:
        text (str): The full text containing the scene description.
        
    Returns:
        str: The extracted "Final specific enhancements and details" section, or None if not found.
    """
    pattern = (
        r"Final specific enhancements.*?\n"       # Match the section header (case-insensitive)
        r"(.*?)(?=\n\s*-\s*Any other information|" # Stop before the next header "Any other information..."
        r"\Z)"                                    # or end of text
    )

    match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    
    if match:
        return match.group(1).strip()
    return None


if __name__ == "__main__":
    prompt = input("What do you want to generate? ")
    response = agent.generate_content(prompt)

    with open('details.txt', 'a') as f:
        f.write(f"\n## Prompt: {prompt}\n")
        f.write(f"\n## Response: \n {response.text}")
        f.write("\n##End of this response\n")
        f.write("-" * 50)

    theme = re.search(r"- Overall impression:\s*(.+?)(?=\n- |\Z)", response.text, re.DOTALL)
    details = convert_text_to_dict(response.text)
    enhancements = parse_enhancements_to_dict(response.text)
    final_touch = re.search(r"- Any other information in this final section:\s*(.+?)(?=\n- |\Z)", response.text, re.DOTALL)

    if not theme:
        print("Error: Could not extract theme from agent response.")
        sys.exit(1)

    print("Extracted the components")

    settings_prompt = f"""
    This is the description for the overall theme, this will be the base -> not objects, rather the background. You can take this as as the setting i.e. the ground, sky, ambience etc. This background space will set the mood for the entire scene. 
    The description of the setting/theme based on which you need to generate the base: {theme.group(1).strip()}
    # Give all the details, do not give anything other than the details, make sure you include numbers whenever necessary, so that it is easier to forward it to the coding agent.
    """
    setting = describer.generate_content(settings_prompt)
    
    with open("setting.txt", "w") as f:
        f.write(f"{setting.text}")
    print("Got the setting description")
    
    settings = extract_sections_by_roman_numerals(setting.text)
    print(f"Found {len(settings)} setting sections")

    max_retries = 3
    
    # Get initial scene context (empty scene)
    print("\n--- Getting initial scene context ---")
    current_scene_context = get_scene_context()
    print("Initial scene context retrieved")
    
    for i, scene in enumerate(settings.keys(), 1):
        print(f"\n--- Processing Scene {i}/{len(settings)} ---")
        scene_desc = settings[scene]
        
        # Get current scene context before generating new elements
        print("Getting current scene context...")
        current_scene_context = get_scene_context()
        
        retries = 0
        previous_error = None
        previous_code = None
        
        while retries <= max_retries:
            try:
                # Prepare the prompt based on whether this is a retry
                if retries == 0:
                    # First attempt - use scene description with context
                    prompt = scene_desc
                else:
                    # Retry attempt - include previous error and code for context
                    prompt = f"""
                    Original scene description: {scene_desc}
                    
                    Previous code that failed:
                    ```
                    {previous_code}
                    ```
                    
                    Error received from Blender:
                    {previous_error}
                    
                    Important: Do not use the attribute or method that caused the error mentioned above.
                    Generate corrected code that avoids this specific error while still fulfilling the original scene description.
                    Do not add any complex lightings, keep it simple.
                    """
                
                # Generate code with scene context
                generated_code = generate(coding_agent, prompt, current_scene_context)
                generated_code = generated_code[10:-4]  # Trim the response as in original code

                print("Generated code:")
                print(generated_code)

                with open("scripts_og.txt", "w", encoding="utf-8") as f:
                    f.write(generated_code)

                print("\n--- Generated Blender Script ---")
                print("------------------------------\n")

                # Send to Blender
                blender_response = send_to_blender(generated_code)
                print("Blender response:", blender_response)

                # Check if successful
                is_successful = False

                if blender_response and blender_response.get("status") == "success":
                    is_successful = True

                if is_successful:
                    print("✅ Script executed successfully and completely in Blender!")
                    # Update scene context after successful execution
                    current_scene_context = get_scene_context()
                    break  # Exit retry loop when successful
                else:
                    # Store the error and code for the next retry
                    previous_error = get_error_details(blender_response)
                    previous_code = generated_code
                    
                    retries += 1
                    if retries <= max_retries:
                        print(f"❌ Script failed or was incomplete. Attempt {retries}/{max_retries + 1}. Retrying with error feedback...\n")
                        print(f"Error details: {previous_error}")
                    else:
                        print(f"❌ Reached maximum retry limit ({max_retries + 1} attempts). Moving to next scene description.")
                        log_final_error(blender_response)

            except Exception as e:
                # Store the exception info for retry context
                previous_error = f"Exception occurred: {str(e)}"
                previous_code = generated_code if 'generated_code' in locals() else None
                
                retries += 1
                print(f"❌ Exception occurred: {str(e)}")
                if retries <= max_retries:
                    print(f"Retrying with error context... Attempt {retries}/{max_retries + 1}\n")
                else:
                    print(f"❌ Reached maximum retry limit due to exceptions. Moving to next scene description.")
                    break

    print("\n🎬 All scene descriptions processed.")

    flag = True
    while flag:
    # Extract and process Key Elements section
        section = extract_key_elements_section(response.text)
        if section:
            elements = get_all_elements(section)
            print(f"\n--- Processing {len(elements)} Key Elements ---")
            
            max_retries = 1
            for i, (category, element_name, description) in enumerate(elements, 1):
                print(f"\n--- Processing Element {i}/{len(elements)}: {element_name} ---")
                print(f"Category: {category}")
                print(f"Description: {description[:100]}...")

                # Get current scene context before processing each element
                print("Getting current scene context...")
                current_scene_context = get_scene_context()

                element_prompt = f"""
        Category: {category}
        Element: {element_name}
        Description: {description}

        Generate Blender code to create this specific element within the overall scene context.
        Consider the existing objects and position this element appropriately.
        """

                retries = 0
                previous_error = None
                previous_code = None
                
                while retries <= max_retries:
                    try:
                        # Prepare the prompt based on whether this is a retry
                        if retries == 0:
                            # First attempt - use element description with context
                            prompt = element_prompt
                        else:
                            # Retry attempt - include previous error and code for context
                            prompt = f"""
                            Original element description:
                            Category: {category}
                            Element: {element_name}
                            Description: {description}
                            
                            Previous code that failed:
                            ```
                            {previous_code}
                            ```
                            
                            Error received from Blender:
                            {previous_error}
                            
                            Important: Do not use the attribute or method that caused the error mentioned above.
                            Generate corrected code that avoids this specific error while still fulfilling the original element description.
                            """
                        
                        # Generate code for this specific element with scene context
                        generated_code = generate(coding_agent, prompt, current_scene_context)
                        generated_code = generated_code[10:-4]  # Trim the response as in original code

                        print("Generated code for element:")
                        print(generated_code)

                        print(f"\n--- Generated Blender Script for {element_name} ---")
                        print("------------------------------\n")

                        # Send to Blender
                        blender_response = send_to_blender(generated_code)
                        print("Blender response:", blender_response)

                        # Check if successful
                        is_successful = False

                        if blender_response and blender_response.get("status") == "success":
                            is_successful = True

                        if is_successful:
                            print(f"✅ {element_name} script executed successfully and completely in Blender!")
                            # Update scene context after successful execution
                            current_scene_context = get_scene_context()
                            break  # Exit retry loop when successful
                        else:
                            # Store the error and code for the next retry
                            previous_error = get_error_details(blender_response)
                            previous_code = generated_code
                            
                            retries += 1
                            if retries <= max_retries:
                                print(f"❌ {element_name} script failed or was incomplete. Attempt {retries}/{max_retries + 1}. Retrying with error feedback...\n")
                                print(f"Error details: {previous_error}")
                            else:
                                print(f"❌ Reached maximum retry limit ({max_retries + 1} attempts) for {element_name}. Moving to next element.")
                                log_final_error(blender_response)

                    except Exception as e:
                        # Store the exception info for retry context
                        previous_error = f"Exception occurred: {str(e)}"
                        previous_code = generated_code if 'generated_code' in locals() else None
                        
                        retries += 1
                        print(f"❌ Exception occurred for {element_name}: {str(e)}")
                        if retries <= max_retries:
                            print(f"Retrying with error context... Attempt {retries}/{max_retries + 1}\n")
                        else:
                            print(f"❌ Reached maximum retry limit due to exceptions for {element_name}. Moving to next element.")
                            break
                            
            print(f"\n🎬 All {len(elements)} key elements processed.")
        else:
            print("❌ No Key Elements and Details section found in the response.")

        enhancements = extract_final_enhancements_section(response.text)

        enhancements_prompt = f"""
        This is the description for the additional enhancements to be made for a scene.
        The description of the enhancements: {enhancements}
        # Give all the details, do not give anything other than the details, make sure you include numbers whenever necessary, so that it is easier to forward it to the coding agent.

        the scene itself for context: {response.text} 
        """
        enhancements = describer.generate_content(enhancements_prompt)
        
        with open("enhancements.txt", "w") as f:
            f.write(f"{enhancements.text}")
        print("Got the setting description")
        
        enhancements = extract_sections_by_roman_numerals(enhancements.text)
        print(f"Found {len(enhancements)} setting sections")

        max_retries = 2
        
        # Get initial scene context (empty scene)
        print("\n--- Getting initial scene context ---")
        current_scene_context = get_scene_context()
        print("Initial scene context retrieved")
        
        for i, scene in enumerate(enhancements.keys(), 1):
            print(f"\n--- Processing Scene {i}/{len(enhancements)} ---")
            scene_desc = enhancements[scene]
            
            print(scene_desc)
            # Get current scene context before generating new elements
            print("Getting current scene context...")
            current_scene_context = get_scene_context()
            
            retries = 0 
            previous_error = None
            previous_code = None
            
            while retries <= max_retries:
                try:
                    # Prepare the prompt based on whether this is a retry
                    if retries == 0:
                        # First attempt - use scene description with context
                        prompt = scene_desc
                    else:
                        # Retry attempt - include previous error and code for context
                        prompt = f"""
                        Original scene description: {scene_desc}
                        
                        Previous code that failed:
                        ```
                        {previous_code}
                        ```
                        
                        Error received from Blender:
                        {previous_error}
                        
                        Important: Do not use the attribute or method that caused the error mentioned above.
                        Generate corrected code that avoids this specific error while still fulfilling the original scene description.
                        """
                    
                    # Generate code with scene context
                    generated_code = generate(coding_agent, prompt, current_scene_context)
                    generated_code = generated_code[10:-4]  # Trim the response as in original code

                    print("Generated code:")
                    print(generated_code)

                    with open("scripts_og.txt", "w", encoding="utf-8") as f:
                        f.write(generated_code)

                    print("\n--- Generated Blender Script ---")
                    print("------------------------------\n")

                    # Send to Blender
                    blender_response = send_to_blender(generated_code)
                    print("Blender response:", blender_response)

                    # Check if successful
                    is_successful = False

                    if blender_response and blender_response.get("status") == "success":
                        is_successful = True

                    if is_successful:
                        print("✅ Script executed successfully and completely in Blender!")
                        # Update scene context after successful execution
                        current_scene_context = get_scene_context()
                        break  # Exit retry loop when successful
                    else:
                        # Store the error and code for the next retry
                        previous_error = get_error_details(blender_response)
                        previous_code = generated_code
                        
                        retries += 1
                        if retries <= max_retries:
                            print(f"❌ Script failed or was incomplete. Attempt {retries}/{max_retries + 1}. Retrying with error feedback...\n")
                            print(f"Error details: {previous_error}")
                        else:
                            print(f"❌ Reached maximum retry limit ({max_retries + 1} attempts). Moving to next scene description.")
                            log_final_error(blender_response)

                except Exception as e:
                    # Store the exception info for retry context
                    previous_error = f"Exception occurred: {str(e)}"
                    previous_code = generated_code if 'generated_code' in locals() else None
                    
                    retries += 1
                    print(f"❌ Exception occurred: {str(e)}")
                    if retries <= max_retries:
                        print(f"Retrying with error context... Attempt {retries}/{max_retries + 1}\n")
                    else:
                        print(f"❌ Reached maximum retry limit due to exceptions. Moving to next scene description.")
                        break

        print("\n🎬 All enhancements completed.")

        custom_angles = [
            ("front", (radians(90), 0, radians(0))),
            ("back",  (radians(90), 0, radians(180))),
            ("iso_left", (radians(60), 0, radians(-45))),
            ("iso_right", (radians(60), 0, radians(45))),
        ]
        take_multiple_screenshots(output_dir="renders/scene", angles=custom_angles)

        review = review_scene_with_images(reviewer_agent, prompt, "renders/scene")
        
        with open("review.txt", "w") as review_file:
            review_file.write(review.text)

        print(type(review), review)

        lines = review.strip().split('\n')

        if "Yes" in lines[0]:
            flag = False
        else:
            flag = True

    effects_prompt = f"This is the description of blender scene: {response.text}. Based on it add some final effects to the scene."

    current_scene_context = get_scene_context()

    max_retries = 2
    retries = 0
    previous_error = None
    previous_code = None

    while retries <= max_retries:
        try:
            # Prepare the prompt based on whether this is a retry
            if retries == 0:
                # First attempt - use scene description with context
                prompt = scene_desc
            else:
                # Retry attempt - include previous error and code for context
                prompt = f"""
                Original scene description: {scene_desc}
                
                Previous code that failed:
                ```
                {previous_code}
                ```
                
                Error received from Blender:
                {previous_error}
                
                Important: Do not use the attribute or method that caused the error mentioned above.
                Generate corrected code that avoids this specific error while still fulfilling the original scene description.
                Do not add any complex lightings, keep it simple.
                """
            
            # Generate code with scene context
            generated_code = generate(effects_agent, prompt, current_scene_context)
            generated_code = generated_code[10:-4]  # Trim the response as in original code

            print("Generated code:")
            print(generated_code)

            with open("effects_script.txt", "w", encoding="utf-8") as f:
                f.write(generated_code)

            print("\n--- Generated Blender Script ---")
            print("------------------------------\n")

            # Send to Blender
            blender_response = send_to_blender(generated_code)
            print("Blender response:", blender_response)

            # Check if successful
            is_successful = False

            if blender_response and blender_response.get("status") == "success":
                is_successful = True

            if is_successful:
                print("✅ Script executed successfully and completely in Blender!")
                # Update scene context after successful execution
                current_scene_context = get_scene_context()
                break  # Exit retry loop when successful
            else:
                # Store the error and code for the next retry
                previous_error = get_error_details(blender_response)
                previous_code = generated_code
                
                retries += 1
                if retries <= max_retries:
                    print(f"❌ Script failed or was incomplete. Attempt {retries}/{max_retries + 1}. Retrying with error feedback...\n")
                    print(f"Error details: {previous_error}")
                else:
                    print(f"❌ Reached maximum retry limit ({max_retries + 1} attempts). Moving to next scene description.")
                    log_final_error(blender_response)

        except Exception as e:
            # Store the exception info for retry context
            previous_error = f"Exception occurred: {str(e)}"
            previous_code = generated_code if 'generated_code' in locals() else None
            
            retries += 1
            print(f"❌ Exception occurred: {str(e)}")
            if retries <= max_retries:
                print(f"Retrying with error context... Attempt {retries}/{max_retries + 1}\n")
            else:
                print(f"❌ Reached maximum retry limit due to exceptions. Moving to next scene description.")
                break

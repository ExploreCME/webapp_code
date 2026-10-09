import json
import os

# 1. This is the exact path we used in your on_demand_generator.py script
output_path = '/home/ps51632/mysite/topics_by_organ_system.json'

# 2. Make sure the folder actually exists before we try to save to it
os.makedirs(os.path.dirname(output_path), exist_ok=True)

# 3. Write your massive dictionary to the JSON file
try:
    with open(output_path, 'w') as json_file:
        # We use indent=4 so the JSON file is easily readable by humans if you open it!
        json.dump(pance_standalone_disease_states, json_file, indent=4)
        
    print(f"✅ Success! All {len(pance_standalone_disease_states)} organ systems and their topics have been saved to:")
    print(output_path)
    
except Exception as e:
    print(f"❌ Error saving file: {e}")
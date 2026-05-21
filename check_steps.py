#!/usr/bin/env python3
import json

output_file = r'c:\Users\Evan\AppData\Roaming\Code\User\workspaceStorage\f5c2a80bc03c924123ecdf9e6e652680\GitHub.copilot-chat\chat-session-resources\cc054952-ee52-44c6-8607-6e304b9d0254\toolu_bdrk_01EdvA5Kk15kTsKSHqqJ1rEt__vscode-1779323807512\content.txt'

with open(output_file, 'r', encoding='utf-8', errors='ignore') as f:
    content = f.read()

# Find the orchestration response section
if 'orchestration_steps' in content or '"steps"' in content:
    # Extract the entire JSON response
    json_start = content.rfind('{')  # Find the last { which should be start of JSON
    if json_start != -1:
        try:
            json_end = content.find('}', json_start)
            # Find the real end by counting braces
            brace_count = 0
            for i in range(json_start, len(content)):
                if content[i] == '{':
                    brace_count += 1
                elif content[i] == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        json_str = content[json_start:i+1]
                        data = json.loads(json_str)
                        
                        # Print steps
                        if 'orchestration_steps' in data:
                            steps = data['orchestration_steps']
                            for step in steps:
                                print(f"\n{'='*80}")
                                print(f"Step {step.get('step', 'N/A')}:")
                                print(f"{'='*80}")
                                print(f"Executed hook calls: {step.get('executed_hook_calls', 0)}")
                                if step.get('results'):
                                    for res in step['results'][:1]:  # Just show first result
                                        print(f"Hook result keys: {list(res.keys())}")
                        
                        # Print final status
                        print(f"\n{'='*80}")
                        print("Final Status:")
                        print(f"{'='*80}")
                        print(f"Stopped reason: {data.get('stopped_reason', 'N/A')}")
                        print(f"Stop step: {data.get('stop_step', 'N/A')}")
                        break
        except Exception as e:
            print(f"Error: {e}")
else:
    print("No orchestration data found")

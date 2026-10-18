#!/usr/bin/env python3
"""
AI agent with integrated tools using Ollama.
"""

import os
import glob
import json
import time
import click
import pathlib
import subprocess
import threading
import dotenv
import requests
from typing import Dict, List, Any
from colorama import Fore, Style, init
from openai import OpenAI


# Initialize colorama
init(autoreset=True)

# Load environment variables
dotenv.load_dotenv()

class Spinner:
    """Spinner animation to show loading state."""
    
    def __init__(self, message="Thinking"):
        self.message = message
        self.spinning = False
        self.spinner_chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self.spinner_thread = None
    
    def spin(self):
        """Display spinner animation."""
        i = 0
        while self.spinning:
            i = (i + 1) % len(self.spinner_chars)
            print(f"\r{Fore.YELLOW}{self.message} {self.spinner_chars[i]} ", end="")
            time.sleep(0.1)
        # Clear the spinner when done
        print("\r" + " " * (len(self.message) + 10) + "\r", end="")
    
    def start(self):
        """Start the spinner in a separate thread."""
        self.spinning = True
        self.spinner_thread = threading.Thread(target=self.spin)
        self.spinner_thread.daemon = True
        self.spinner_thread.start()
    
    def stop(self):
        """Stop the spinner."""
        self.spinning = False
        if self.spinner_thread:
            self.spinner_thread.join()


class CodingAgent:

    def __init__(self, model: str = "qwen2.5-coder:3b", debug: bool = False):
        """Initialize the agent."""
        self.model = model

        self.client = OpenAI(
            base_url='http://localhost:11434/v1/',
            api_key='ollama',
        )
        self.messages = []
        self.spinner = Spinner()
        self.current_directory = pathlib.Path.cwd()
        self.debug = debug
    
    def run(self):
        """Run the agent's main loop."""
        print(f"{Fore.CYAN}Agent initialized with model: {self.model}")
        print(f"{Fore.CYAN}Type 'exit' or 'quit' to end the conversation.")
        print(f"{Fore.CYAN}Type 'debug' to toggle debug mode (currently {self.debug}).")
        
        # Add system message
        self.messages.append({"role": "system", "content": self.get_system_prompt()})
        
        while True:
            try:
                # Get user input
                user_input = input(f"\n{Fore.GREEN}You: {Style.RESET_ALL}")
                
                # Check for exit
                if user_input.lower() in ["exit", "quit"]:
                    print(f"{Fore.CYAN}Exiting...")
                    break
                
                # Check for debug toggle
                if user_input.lower() == "debug":
                    self.debug = not self.debug
                    print(f"{Fore.CYAN}Debug mode: {self.debug}")
                    continue
                
                # Show spinner while processing
                self.spinner.start()
                
                # Process the input and get response
                response = self.process_user_input(user_input)
                
                # Stop spinner
                self.spinner.stop()
                
                # Display response
                print(f"\n{Fore.BLUE}Agent: {Style.RESET_ALL}{response}")
                
            except KeyboardInterrupt:
                self.spinner.stop()
                print(f"\n{Fore.CYAN}Exiting...")
                break
                
            except Exception as e:
                self.spinner.stop()
                print(f"\n{Fore.RED}Error: {str(e)}")
    
    def process_user_input(self, user_input: str) -> str:
        """Process user input and handle tool calls."""
        # Add user message
        self.messages.append({"role": "user", "content": user_input})
        
        # Initialize response placeholder
        final_response = ""
        
        for _ in range(5):  # Limit iterations to prevent infinite loops
            # Get response from model
            completion = self.chat(self.messages)
            
            # Extract message
            response_message = completion.choices[0].message
            message_content = response_message.content or ""
            
            # Add to messages history
            self.messages.append(response_message.model_dump())
            
            # Check for native tool calls
            if hasattr(response_message, 'tool_calls') and response_message.tool_calls:
                for tool_call in response_message.tool_calls:
                    function_name = tool_call.function.name
                    try:
                        function_args = json.loads(
                            self._repair_json_strings(tool_call.function.arguments)
                        )
                    except (json.JSONDecodeError, TypeError):
                        function_args = {}
                    
                    if self.debug:
                        print(f"\n{Fore.YELLOW}DEBUG - Tool call detected: {function_name}")
                        print(f"Arguments: {json.dumps(function_args, indent=2)}")
                    
                    # Execute the tool
                    self.spinner.message = f"Using {function_name}"
                    tool_result = self.execute_tool(function_name, function_args)
                    
                    # Add tool result to messages
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": function_name,
                        "content": tool_result
                    })
                
                # Continue the conversation to get the final response
                continue
            
            # Some models (e.g. qwen2.5-coder:3b) emit tool calls as JSON text
            # inside the message content instead of native tool_calls. Parse those.
            parsed_tool_call = self.parse_text_tool_call(message_content)
            if parsed_tool_call:
                function_name, function_args = parsed_tool_call
                
                if self.debug:
                    print(f"\n{Fore.YELLOW}DEBUG - Text tool call detected: {function_name}")
                    print(f"Arguments: {json.dumps(function_args, indent=2)}")
                
                # Execute the tool
                self.spinner.message = f"Using {function_name}"
                tool_result = self.execute_tool(function_name, function_args)
                
                # Feed the result back and ask for a final answer
                self.messages.append({
                    "role": "user",
                    "content": (
                        f"Tool '{function_name}' returned:\n{tool_result}\n\n"
                        "Based on that result, give the final answer. "
                        "Do not output another tool call."
                    )
                })
                continue
            
            # No tool calls, this is the final response
            final_response = message_content
            break
        
        return final_response
    
    @staticmethod
    def _repair_json_strings(text: str) -> str:
        """Escape raw newlines/tabs that appear inside JSON string values.

        Small coder models often emit pretty-printed JSON where multi-line
        string values (e.g. file ``content``) contain literal newlines, which
        is invalid JSON and would otherwise defeat json.loads.
        """
        out = []
        in_string = False
        escaped = False
        control_escape = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
        for ch in text:
            if escaped:
                out.append(ch)
                escaped = False
                continue
            if ch == "\\":
                out.append(ch)
                escaped = True
                continue
            if ch == '"':
                in_string = not in_string
                out.append(ch)
                continue
            if in_string and ch in control_escape:
                out.append(control_escape[ch])
                continue
            out.append(ch)
        return "".join(out)

    def parse_text_tool_call(self, content: str):
        """Parse a tool call that the model emitted as JSON text."""
        if not content:
            return None
        
        text = content.strip()
        
        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.splitlines()[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        # Repair literal newlines inside JSON strings
        text = self._repair_json_strings(text)
        
        # Try the whole text and the outermost JSON object
        candidates = [text]
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            candidates.append(text[start:end + 1])
        
        for candidate in candidates:
            try:
                data = json.loads(candidate)
            except (json.JSONDecodeError, TypeError):
                continue
            
            if not isinstance(data, dict) or "name" not in data:
                continue
            
            function_name = data["name"]
            function_args = data.get("arguments", data.get("parameters", {}))
            
            if isinstance(function_args, str):
                try:
                    function_args = json.loads(function_args)
                except json.JSONDecodeError:
                    function_args = {}
            
            if not isinstance(function_args, dict):
                function_args = {}
            
            return function_name, function_args
        
        return None
    
    def get_system_prompt(self) -> str:
        return f"""You are an AI assistant that uses tools for file operations, code analysis, and commands. Give precise and concise answers.
        
Current directory: {self.current_directory}

Tool Usage Rules:
1. ALWAYS use write_file for new file creation
2. Use read_file for reading existing files
3. Use list_files to browse directories
4. Use run_command for system operations
5. ALWAYS use web_search for any questions about current events, facts, data, or information that may be time-sensitive or outside your training data
6. When showing code, include the full file content
7. Always write clean, readable, well-formatted code with correct, consistent indentation (4 spaces per level), blank lines between definitions, and proper spacing around operators
8. Output code as a single-line JSON string with escaped newlines (\\n), never raw newlines inside JSON string values

Think step-by-step:
1. Analyze the request
2. Choose appropriate tools
3. Execute tools in order
4. Verify results

Respond ONLY with tool calls or final answers."""
    
    def chat(self, messages):
        """Send a chat request to the Ollam API."""
        tools = self.get_tools_definition()
        
        if self.debug:
            print(f"\n{Fore.YELLOW}DEBUG - Sending {len(messages)} messages to Ollama API")
            
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto"
        )
        
        if self.debug:
            print(f"\n{Fore.YELLOW}DEBUG - Received response from Ollama API")
            
        return response
    
    def get_tools_definition(self) -> List[Dict[str, Any]]:
        """Define tools in the format expected by the Ollama API."""
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read the contents of a file",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Path to the file to read"
                            }
                        },
                        "required": ["path"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Write content to a file",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Path to the file to write"
                            },
                            "content": {
                                "type": "string",
                                "description": "Clean, well-formatted, properly indented code to write to the file"
                            }
                        },
                        "required": ["path", "content"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "list_files",
                    "description": "List files in a directory",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Path to the directory (defaults to current directory)"
                            }
                        },
                        "required": []
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "find_files",
                    "description": "Find files matching a pattern",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pattern": {
                                "type": "string",
                                "description": "Glob pattern to match"
                            }
                        },
                        "required": ["pattern"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "run_command",
                    "description": "Run a shell command",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "cmd": {
                                "type": "string",
                                "description": "Command to run"
                            }
                        },
                        "required": ["cmd"]
                    }
                }
            },
{
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web for information",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query"
                },
                "num_results": {
                    "type": "integer",
                    "description": "Number of results to return (max 10, default 5)"
                }
            },
            "required": ["query"]
        }
    }
}
            
        ]
        return tools
    
    def execute_tool(self, tool_name: str, params: Dict[str, Any]) -> str:
        """Execute a tool based on name and parameters."""
        tools = {
            "read_file": self.read_file,
            "write_file": self.write_file,
            "list_files": self.list_files,
            "find_files": self.find_files,
            "run_command": self.run_command,
            "web_search": self.web_search 
        }
        
        if tool_name in tools:
            try:
                return tools[tool_name](**params)
            except Exception as e:
                return f"{Fore.RED}Error executing {tool_name}: {str(e)}{Style.RESET_ALL}"
        else:
            return f"{Fore.RED}Tool '{tool_name}' not implemented{Style.RESET_ALL}"
    
    # Tool implementations
    def read_file(self, path: str) -> str:
        """Read a file's contents."""
        try:
            file_path = (self.current_directory / path).resolve()
            content = file_path.read_text(encoding='utf-8', errors='replace')
            return f"Content of {path}:\n{content}"
        except Exception as e:
            return f"{Fore.RED}Error reading file {path}: {str(e)}{Style.RESET_ALL}"
    
    def write_file(self, path: str, content: str) -> str:
        """Write content to a file."""
        try:
            file_path = (self.current_directory / path).resolve()
            file_path.parent.mkdir(parents=True, exist_ok=True)
            # Normalize line endings and ensure trailing newline
            text = content
            # Fix common indentation issues if content came with mixed tabs/spaces
            # Ensure consistent newline at end
            if not text.endswith('\n'):
                text = text + '\n'
            file_path.write_text(text, encoding='utf-8')
            return f"{Fore.GREEN}✅ Successfully created file {path}. The file now contains {len(text)} characters.{Style.RESET_ALL}"
        except Exception as e:
            return f"{Fore.RED}Error writing to file {path}: {str(e)}{Style.RESET_ALL}"
    
    def list_files(self, path: str = ".") -> str:
        """List files in a directory."""
        try:
            dir_path = (self.current_directory / path).resolve()
            if not dir_path.is_dir():
                return f"{Fore.RED}Error: {path} is not a directory{Style.RESET_ALL}"
                
            items = list(dir_path.iterdir())
            
            dirs = []
            files = []
            
            for item in items:
                if item.is_dir():
                    dirs.append(f"{Fore.CYAN}📁 {item.name}/{Style.RESET_ALL}")
                else:
                    files.append(f"{Fore.YELLOW}📄 {item.name}{Style.RESET_ALL}")
                    
            all_items = sorted(dirs) + sorted(files)
            result = f"Contents of {dir_path}:\n" + "\n".join(all_items)
            
            return result
        except Exception as e:
            return f"{Fore.RED}Error listing files in {path}: {str(e)}{Style.RESET_ALL}"
    
    def find_files(self, pattern: str) -> str:
        """Find files matching a pattern."""
        try:
            files = list(glob.glob(pattern, recursive=True))
            if files:
                file_list = "\n".join([f"{Fore.YELLOW}{f}{Style.RESET_ALL}" for f in files])
                return f"{Fore.GREEN}Found {len(files)} files matching '{pattern}':{Style.RESET_ALL}\n{file_list}"
            else:
                return f"{Fore.YELLOW}No files found matching '{pattern}'{Style.RESET_ALL}"
        except Exception as e:
            return f"{Fore.RED}Error finding files with pattern '{pattern}': {str(e)}{Style.RESET_ALL}"
    
    def run_command(self, cmd: str) -> str:
        """Run a shell command."""
        try:
            # Limit commands for security
            allowed_commands = [
                'ls', 'dir', 'find', 'grep', 'cat', 'head', 'tail', 'wc', 'echo',
                'python', 'python3', 'pip', 'pip3', 'pytest', 'pwd', 'mkdir', 'touch'
            ]
            
            cmd_parts = cmd.split()
            if not cmd_parts or cmd_parts[0] not in allowed_commands:
                return f"{Fore.RED}Error: Command '{cmd_parts[0] if cmd_parts else ''}' is not allowed for security reasons{Style.RESET_ALL}"
            
            # Run the command
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                output = result.stdout
                if not output:
                    return f"{Fore.GREEN}✅ Command '{cmd}' executed successfully with no output{Style.RESET_ALL}"
                return f"{Fore.GREEN}✅ Command output:{Style.RESET_ALL}\n{output}"
            else:
                return f"{Fore.RED}❌ Command failed with error:{Style.RESET_ALL}\n{result.stderr}"
        except Exception as e:
            return f"{Fore.RED}Error executing command '{cmd}': {str(e)}{Style.RESET_ALL}"
    
    def web_search(self, query: str, num_results: int = 5) -> str:
        """Search the web using Exa API."""
        try:
            api_key = os.environ.get("EXA_API_KEY")
            
            if not api_key:
                return "Error: Exa API key not configured."
            
            # Exa API endpoint
            url = "https://api.exa.ai/search"
            headers = {"Content-Type": "application/json", "x-api-key": api_key}
            data = {"query": query, "num_results": int(num_results), "use_autoprompt": True}
            
            # Make the request
            response = requests.post(url, headers=headers, json=data)
            
            if response.status_code != 200:
                return f"Error: Exa API returned status code {response.status_code}"
            
            # Parse the results
            search_results = response.json()
            
            if "results" not in search_results or not search_results["results"]:
                return f"No results found for query: '{query}'"
            
            # Format the results
            formatted_results = f"Search results for '{query}':\n\n"
            for i, item in enumerate(search_results["results"], 1):
                title = item.get("title", "No title")
                url = item.get("url", "No link")
                text = item.get("text", "")
                snippet = text[:150] + "..." if len(text) > 150 else text
                
                formatted_results += f"{i}. {title}\n"
                formatted_results += f"   URL: {url}\n"
                formatted_results += f"   Description: {snippet}\n\n"
            
            return formatted_results
            
        except Exception as e:
            return f"Error executing web search: {str(e)}"
    


@click.command()
@click.option("--model", default="qwen2.5-coder:3b", help="The Ollama model to use.")
@click.option("--debug", is_flag=True, help="Enable debug mode.")
def main(model: str, debug: bool):
    """Run the Coding Agent."""
    agent = CodingAgent(model=model, debug=debug)
    agent.run()


if __name__ == "__main__":
    main()

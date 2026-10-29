#!/usr/bin/env python3
"""
Secure autonomous coding agent.

Writes files into a shared local workspace and executes all terminal
commands inside an ephemeral, resource-constrained Docker container.
"""

import os
import re
import json
import shlex
import tempfile
import subprocess

import llm_config

client = llm_config.client
MODEL_NAME = llm_config.MODEL_NAME
# Cap generation so a looping model cannot hang the graph.
LLM_MAX_TOKENS = llm_config.LLM_MAX_TOKENS
WORKSPACE_DIR = os.path.abspath(os.path.expanduser("~/data/pt87/ai_coding/agent_workspace"))
DOCKER = os.environ.get("DOCKER_CMD", "docker").split()

# The sandbox has no network, so dependencies must be baked into the image.
# By default we auto-build this image once from SANDBOX_BASE + SANDBOX_PACKAGES.
SANDBOX_IMAGE = os.environ.get("SANDBOX_IMAGE", "local-agent-sandbox:latest")
SANDBOX_BASE = os.environ.get("SANDBOX_BASE", "python:3.11-slim")
SANDBOX_PACKAGES = os.environ.get(
    "SANDBOX_PACKAGES",
    "numpy pandas matplotlib scipy scikit-learn pytest",
)
SANDBOX_AUTOBUILD = os.environ.get("SANDBOX_AUTOBUILD", "1") == "1"

# Envelope the agent operates within (all overridable via env).
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "30"))
SANDBOX_TIMEOUT = int(os.environ.get("SANDBOX_TIMEOUT", "120"))
SANDBOX_MEMORY = os.environ.get("SANDBOX_MEMORY", "2g")

# Third-party dependencies live in a persistent virtualenv on the host.
# Installs run WITH network into that venv; every execution run mounts it
# read-only and puts it on PATH, so executed code itself stays offline.
VENV_DIR = os.path.abspath(
    os.path.expanduser(os.environ.get("SANDBOX_VENV_DIR", "~/.cache/local-cursor/venv"))
)
VENV_MOUNT = "/venv"
INSTALL_TIMEOUT = int(os.environ.get("SANDBOX_INSTALL_TIMEOUT", "300"))
TEST_TIMEOUT = int(os.environ.get("SANDBOX_TEST_TIMEOUT", "180"))
PKG_SPEC_RE = re.compile(r"^[A-Za-z0-9._+\-\[\]<>=!~,]+$")


def _resolve_in_workspace(filepath: str) -> str:
    """Resolve a path and guarantee it stays inside the workspace."""
    safe_path = os.path.abspath(os.path.join(WORKSPACE_DIR, filepath.lstrip("/")))
    if safe_path != WORKSPACE_DIR and not safe_path.startswith(WORKSPACE_DIR + os.sep):
        raise ValueError("Path outside workspace context")
    return safe_path


def _coerce_content(content) -> str:
    """Best-effort conversion of a model-supplied value into file text."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        for key in ("content", "code", "text", "description", "value", "body"):
            if isinstance(content.get(key), str):
                return content[key]
        return json.dumps(content)
    if content is None:
        return ""
    return str(content)


def write_workspace_file(filepath: str, content) -> str:
    """Securely write a file into the shared local workspace mount."""
    try:
        content = _coerce_content(content)
        safe_path = _resolve_in_workspace(filepath)
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)
        with open(safe_path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"[Success: Written to {filepath} in container workspace]"
    except Exception as e:
        return f"[Error writing file: {str(e)}]"


def read_workspace_file(filepath: str) -> str:
    """Read a file from the shared workspace."""
    try:
        safe_path = _resolve_in_workspace(filepath)
        if not os.path.isfile(safe_path):
            return f"[Error: File not found: {filepath}]"
        with open(safe_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return f"Content of {filepath}:\n{content}"
    except Exception as e:
        return f"[Error reading file: {str(e)}]"


def list_workspace_files(path: str = ".") -> str:
    """List files and directories in a workspace folder."""
    try:
        safe_path = _resolve_in_workspace(path)
        if not os.path.isdir(safe_path):
            return f"[Error: Not a directory: {path}]"
        entries = sorted(os.listdir(safe_path))
        if not entries:
            return f"[{path} is empty]"
        lines = []
        for name in entries:
            suffix = "/" if os.path.isdir(os.path.join(safe_path, name)) else ""
            lines.append(f"{name}{suffix}")
        return f"Contents of {path}:\n" + "\n".join(lines)
    except Exception as e:
        return f"[Error listing files: {str(e)}]"


def edit_workspace_file(
    filepath: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """Replace an exact string in a workspace file."""
    try:
        safe_path = _resolve_in_workspace(filepath)
        if not os.path.isfile(safe_path):
            return f"[Error: File not found: {filepath}]"
        with open(safe_path, "r", encoding="utf-8") as f:
            content = f.read()

        occurrences = content.count(old_string)
        if occurrences == 0:
            return f"[Error: old_string not found in {filepath}]"
        if occurrences > 1 and not replace_all:
            return (
                f"[Error: old_string appears {occurrences} times in {filepath}; "
                "make it unique or set replace_all=true]"
            )

        if replace_all:
            content = content.replace(old_string, new_string)
        else:
            content = content.replace(old_string, new_string, 1)

        with open(safe_path, "w", encoding="utf-8") as f:
            f.write(content)

        replaced = occurrences if replace_all else 1
        return f"[Success: Edited {filepath} ({replaced} replacement(s))]"
    except Exception as e:
        return f"[Error editing file: {str(e)}]"


def ensure_sandbox_image() -> bool:
    """Build the sandbox image on first use (it is offline at runtime).

    Dependencies must live in the image because the container runs with
    `--network none`, so an in-container `pip install` can never work.
    """
    if not SANDBOX_AUTOBUILD:
        return True

    inspect = subprocess.run(
        [*DOCKER, "image", "inspect", SANDBOX_IMAGE],
        capture_output=True, text=True,
    )
    if inspect.returncode == 0:
        return True

    print(
        f"  Sandbox image '{SANDBOX_IMAGE}' not found. "
        "Building it once (host needs internet)..."
    )
    build_dir = tempfile.mkdtemp(prefix="agent-sandbox-")
    dockerfile = os.path.join(build_dir, "Dockerfile")
    with open(dockerfile, "w", encoding="utf-8") as f:
        f.write(
            f"FROM {SANDBOX_BASE}\n"
            f"RUN pip install --no-cache-dir {SANDBOX_PACKAGES}\n"
        )

    try:
        res = subprocess.run(
            [*DOCKER, "build", "-t", SANDBOX_IMAGE, "-f", dockerfile, build_dir],
            capture_output=True, text=True, timeout=1800,
        )
    except Exception as e:
        print(f"  Failed to build sandbox image: {e}")
        return False

    if res.returncode != 0:
        print(f"  Failed to build sandbox image:\n{res.stderr[-1500:]}")
        return False

    print(f"  Sandbox image '{SANDBOX_IMAGE}' is ready.")
    return True


def _read_requirements(rel_path: str) -> list:
    """Read install specs from a requirements file inside the workspace."""
    safe_path = _resolve_in_workspace(rel_path)
    if not os.path.isfile(safe_path):
        raise ValueError(f"requirements file not found: {rel_path}")
    specs = []
    with open(safe_path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            specs.append(line)
    return specs


def install_dependencies(packages) -> str:
    """Install dependencies into the persistent sandbox venv.

    This is the ONLY step that touches the network; it creates/updates a
    virtualenv on the host. Runtime executions mount it read-only offline.

    `packages` may be a list of specifiers or a space/comma separated string.
    """
    if isinstance(packages, str):
        specs = [s for s in re.split(r"[,\s]+", packages) if s]
    elif isinstance(packages, (list, tuple)):
        specs = [str(s).strip() for s in packages if str(s).strip()]
    else:
        return f"[Error: invalid packages argument: {packages!r}]"

    if not specs:
        return "[Error: no packages specified]"

    unsafe = [s for s in specs if not PKG_SPEC_RE.match(s)]
    if unsafe:
        return f"[Error: refused unsafe package specifier(s): {unsafe}]"

    os.makedirs(VENV_DIR, exist_ok=True)
    print(f"  Installing into sandbox venv: {' '.join(specs)}")

    # Create the venv if needed (idempotent), then pip install into it.
    # Specs are passed as positional args so there is no shell injection.
    script = (
        f"python -m venv --system-site-packages {VENV_MOUNT} >/dev/null 2>&1 || true; "
        f'exec {VENV_MOUNT}/bin/python -m pip install --no-cache-dir --upgrade "$@"'
    )
    docker_cmd = [
        *DOCKER, "run", "--rm",
        "-v", f"{VENV_DIR}:{VENV_MOUNT}",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-e", "HOME=/tmp",
        "--memory", SANDBOX_MEMORY,
        SANDBOX_IMAGE,
        "bash", "-c", script, "_",
        *specs,
    ]

    try:
        res = subprocess.run(
            docker_cmd, capture_output=True, text=True, timeout=INSTALL_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        return f"[Error: dependency install timed out after {INSTALL_TIMEOUT}s]"
    except FileNotFoundError:
        return "[Error: Docker is not installed or not on PATH on the host]"
    except Exception as e:
        return f"[Install Error: {str(e)}]"

    tail = (res.stdout[-2000:] + res.stderr[-2000:]).strip()
    installed = "Successfully installed" in res.stdout or "already satisfied" in res.stdout
    if res.returncode == 0 or installed:
        return f"[Success: installed {' '.join(specs)} into the sandbox venv]\n{tail}"
    return f"[Error: pip install failed (exit {res.returncode})]\n{tail}"


def _parse_pip_install(command: str):
    """If `command` is a pip install, return its list of specs, else None."""
    tokens = command.strip().split()
    if not tokens:
        return None

    low = [t.lower() for t in tokens]
    is_pip = low[0] in ("pip", "pip3", "sudo")
    is_python_m = (
        low[0] in ("python", "python3")
        and len(low) >= 3
        and low[1] == "-m"
        and low[2] in ("pip", "pip3")
    )
    if not (is_pip or is_python_m) or "install" not in low:
        return None

    specs = []
    i = low.index("install") + 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-r", "--requirement"):
            i += 1
            if i < len(tokens):
                specs.extend(_read_requirements(tokens[i]))
            i += 1
            continue
        if tok.startswith("-"):
            i += 1
            continue
        specs.append(tok)
        i += 1
    return specs


def _sandbox_run(command, timeout: int):
    """Run a command in the offline sandbox (shared by all exec tools).

    `command` may be a shell string or an argv list (the latter avoids any
    shell parsing for arguments supplied by the model).
    """
    os.makedirs(VENV_DIR, exist_ok=True)
    tail = ["bash", "-c", command] if isinstance(command, str) else list(command)
    docker_cmd = [
        *DOCKER, "run", "--rm",
        "-v", f"{WORKSPACE_DIR}:/workspace",
        "-v", f"{VENV_DIR}:{VENV_MOUNT}:ro",
        "-w", "/workspace",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-e", "HOME=/tmp",
        "-e", f"PATH={VENV_MOUNT}/bin:/usr/local/bin:/usr/bin:/bin",
        "-e", f"VIRTUAL_ENV={VENV_MOUNT}",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "--network", "none",
        "--memory", SANDBOX_MEMORY,
        SANDBOX_IMAGE,
        *tail,
    ]
    return subprocess.run(
        docker_cmd, capture_output=True, text=True, timeout=timeout
    )


def run_sandbox_command(command: str) -> str:
    """Execute a command inside an ephemeral, isolated Docker sandbox."""
    try:
        # Route `pip install ...` to the network-enabled package installer so
        # the agent can add libraries even though execution runs offline.
        try:
            pip_specs = _parse_pip_install(command)
        except ValueError as e:
            return f"[Error: {e}]"
        if pip_specs is not None:
            if not pip_specs:
                return "[Info: no packages found to install]"
            return install_dependencies(pip_specs)

        print(f"  Sandboxing command: {command}")
        res = _sandbox_run(command, SANDBOX_TIMEOUT)
        return (
            f"[Container Exit Code: {res.returncode}]\n"
            f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        )
    except subprocess.TimeoutExpired:
        return f"[Error: Command execution timed out after {SANDBOX_TIMEOUT} seconds inside the sandbox]"
    except FileNotFoundError:
        return "[Error: Docker is not installed or not on PATH on the host]"
    except Exception as e:
        return f"[Sandbox Execution Error: {str(e)}]"


def _summarize_pytest(stdout: str, stderr: str) -> str:
    """Extract the pytest result line, e.g. '2 passed' / '1 failed, 3 passed'."""
    for text in (stdout, stderr):
        for line in reversed(text.splitlines()):
            stripped = line.strip().strip("=").strip()
            if re.search(r"\d+ (passed|failed|error)", stripped, re.IGNORECASE):
                return stripped
    if "no tests ran" in (stdout + stderr):
        return "no tests ran"
    return "no pytest summary found"


def run_tests(target: str = ".", args: str = "") -> str:
    """Run pytest in the sandbox and return a concise, debuggable result.

    `target` is a file/dir inside the workspace; `args` is optional extra
    pytest flags (e.g. '-k add'). Output is fed back into the model so it can
    patch code until the suite passes.
    """
    if target and (".." in target.split("/") or target.startswith("/")):
        return f"[Error: refused unsafe test target: {target}]"

    argv = ["python", "-m", "pytest", "-q", "--color=no"]
    if args:
        try:
            argv += shlex.split(args)
        except ValueError as e:
            return f"[Error: bad test args: {e}]"
    if target and target != ".":
        argv.append(target)

    print(f"  Running tests: {' '.join(argv[2:])}")
    try:
        res = _sandbox_run(argv, TEST_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"[Error: tests timed out after {TEST_TIMEOUT}s]"
    except FileNotFoundError:
        return "[Error: Docker is not installed or not on PATH on the host]"
    except Exception as e:
        return f"[Test Error: {str(e)}]"

    summary = _summarize_pytest(res.stdout, res.stderr)
    status = "PASSED" if res.returncode == 0 else "FAILED"
    # Keep the context window manageable while preserving the failure details.
    body = (res.stdout + res.stderr).strip()
    if len(body) > 6000:
        body = body[:3000] + "\n... [truncated] ...\n" + body[-3000:]
    return (
        f"[TESTS {status} · exit {res.returncode} · {summary}]\n"
        f"{body}"
    )


tools_map = {
    "write_workspace_file": write_workspace_file,
    "read_workspace_file": read_workspace_file,
    "list_workspace_files": list_workspace_files,
    "edit_workspace_file": edit_workspace_file,
    "install_dependencies": install_dependencies,
    "install_python_package": install_dependencies,  # backward-compatible alias
    "run_sandbox_command": run_sandbox_command,
    "run_tests": run_tests,
}

tools_definition = [
    {
        "type": "function",
        "function": {
            "name": "write_workspace_file",
            "description": "Create or overwrite a file inside the isolated sandbox workspace folder.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "e.g., script.py or app/main.py"},
                    "content": {"type": "string", "description": "Full file content"},
                },
                "required": ["filepath", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_workspace_file",
            "description": "Read the full contents of a file from the sandbox workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "e.g., script.py or app/main.py"}
                },
                "required": ["filepath"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_workspace_files",
            "description": "List the files and subdirectories in a sandbox workspace folder.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Folder relative to workspace root (defaults to '.')"}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_workspace_file",
            "description": "Replace an exact string in an existing workspace file. Prefer this over rewriting whole files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "File to edit, relative to workspace root"},
                    "old_string": {"type": "string", "description": "Exact text to replace"},
                    "new_string": {"type": "string", "description": "Replacement text"},
                    "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)"},
                },
                "required": ["filepath", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "install_dependencies",
            "description": (
                "Install one or more Python dependencies (e.g. 'requests' or 'pandas==2.2') "
                "into a persistent virtualenv shared by all sandbox runs. This is the "
                "only step with internet access. Use it when the code needs a library "
                "that is not already available."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "packages": {
                        "type": "string",
                        "description": "Space or comma separated package specifiers, e.g. 'requests beautifulsoup4'",
                    }
                },
                "required": ["packages"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_sandbox_command",
            "description": "Execute a bash shell command inside an isolated Ubuntu-based python sandbox container.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "e.g., 'python script.py' or 'ls -la'"}
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": (
                "Run the pytest suite inside the isolated sandbox and return the "
                "pass/fail summary plus stdout/stderr. Use this as your TDD check: "
                "after writing code and its test_*.py file, call run_tests; if it "
                "fails, read the output, fix the code, and call it again until it "
                "passes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "File or folder to test inside the workspace (default '.' = whole workspace)",
                    },
                    "args": {
                        "type": "string",
                        "description": "Optional extra pytest flags, e.g. '-k add' or '-x'",
                    },
                },
                "required": [],
            },
        },
    },
]


def _repair_json_strings(text: str) -> str:
    """Escape raw newlines/tabs that appear inside JSON string values.

    Small coder models often emit pretty-printed JSON where multi-line string
    values (e.g. file ``content``) contain literal newlines, which is invalid
    JSON and would otherwise defeat `json.JSONDecoder().raw_decode`.
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


def parse_text_tool_calls(content: str):
    """Parse tool calls that the model emitted as JSON text.

    Some models emit tool calls as JSON text inside the message content
    rather than through native `tool_calls`. A single message may contain
    several concatenated JSON objects, so scan for all of them.

    Returns a list of ``(name, arguments)`` tuples.
    """
    if not content:
        return []

    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    text = _repair_json_strings(text)

    calls = []
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(text):
        start = text.find("{", idx)
        if start == -1:
            break
        try:
            data, end = decoder.raw_decode(text, start)
        except (json.JSONDecodeError, TypeError):
            idx = start + 1
            continue
        idx = end

        if not isinstance(data, dict) or not isinstance(data.get("name"), str):
            continue

        args = data.get("arguments", data.get("parameters", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        calls.append((data["name"], args))

    return calls


def _run_tool(name: str, args: dict) -> str:
    """Look up and run a tool, returning its textual result."""
    tool = tools_map.get(name)
    if tool is None:
        return f"[Error: Tool '{name}' not implemented]"
    try:
        return tool(**args)
    except TypeError as e:
        return f"[Error: Bad arguments for '{name}': {str(e)}]"
    except Exception as e:
        return f"[Error executing '{name}': {str(e)}]"


def run_secure_agent(user_prompt: str):
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    ensure_sandbox_image()

    messages = [
        {
            "role": "system",
            "content": (
                "You are an autonomous senior coding agent running inside a secure "
                "Docker container workspace mounted at /workspace. Use your tools to "
                "build code, check outputs, and fix errors until the task is complete. "
                "Explore with list_workspace_files and read_workspace_file before "
                "editing. Create files with write_workspace_file and make targeted "
                "changes to existing files with edit_workspace_file instead of "
                "rewriting them. Run code with run_sandbox_command. "
                "Follow a TEST-DRIVEN workflow for every coding task: (1) implement "
                "the code, (2) write a pytest test file named test_*.py that asserts "
                "the required behavior, (3) call run_tests. If run_tests reports a "
                "failure, read the stdout/stderr it returns, fix the code (or the "
                "test if the test itself is wrong) with edit_workspace_file, and call "
                "run_tests again. Repeat until the suite passes. Do not claim success "
                "without a passing run_tests result. "
                "The execution container is OFFLINE (no internet): it can only import "
                "packages that are preinstalled or already installed in the sandbox "
                "cache. These packages are ALREADY installed: "
                f"{SANDBOX_PACKAGES}. "
                "If your code needs a third-party library that is not available, call "
                "install_dependencies with the package name(s) first, then run the "
                "code again (installed packages are importable in later runs). Never "
                "call `pip install` through run_sandbox_command; use the installer tool. "
                "When the task is done, reply with a short plain-text summary (no JSON, "
                "no tool call)."
            ),
        },
        {"role": "user", "content": user_prompt},
    ]

    last_signature = None
    repeat_count = 0

    for step in range(MAX_STEPS):
        print(f"\n--- Processing Step {step + 1} ---")

        response = llm_config.chat_completion(
            messages,
            model=MODEL_NAME,
            tools=tools_definition,
            tool_choice="auto",
            temperature=0.2,
            max_tokens=LLM_MAX_TOKENS,
        )

        response_message = response.choices[0].message
        messages.append(llm_config.assistant_message(response_message))

        tool_calls = response_message.tool_calls or []
        text_calls = []
        if not tool_calls:
            text_calls = parse_text_tool_calls(response_message.content or "")

        if not tool_calls and text_calls:
            real_calls = [(n, a) for n, a in text_calls if n in tools_map]
            if not real_calls:
                # Model used a pseudo-tool (e.g. final_answer) to signal it
                # is done. Treat its message as the final answer.
                print(f"\n  Mission Complete. Final Agent Output:\n{response_message.content}")
                break
        elif tool_calls:
            real_calls = []
            for tool_call in tool_calls:
                real_calls.append((
                    tool_call.function.name,
                    json.loads(tool_call.function.arguments or "{}"),
                    tool_call.id,
                ))
        else:
            print(f"\n  Mission Complete. Final Agent Output:\n{response_message.content}")
            break

        # Detect the model repeating the exact same calls with no progress.
        signature = tuple(sorted(
            (c[0], json.dumps(c[1], sort_keys=True)) for c in real_calls
        ))
        if signature == last_signature:
            repeat_count += 1
        else:
            repeat_count = 0
            last_signature = signature
        if repeat_count >= 2:
            print(
                "\n  Stopped: the model repeated the same tool calls "
                f"{repeat_count + 1} times without progress."
            )
            break

        results = []
        for call in real_calls:
            name, args = call[0], call[1]
            print(f"  Agent requested: {name}({args})")
            output = _run_tool(name, args)
            print(f"  Tool result:\n{output}")
            if len(call) == 3:  # native tool call carries an id
                messages.append({
                    "tool_call_id": call[2],
                    "role": "tool",
                    "name": name,
                    "content": output,
                })
            else:
                results.append(f"Tool '{name}' returned:\n{output}")

        if results:
            messages.append({
                "role": "user",
                "content": (
                    "\n\n".join(results)
                    + "\n\nContinue: either call the next tool or give the final answer."
                ),
            })
    else:
        print(f"\n  Reached max steps ({MAX_STEPS}) without a final answer.")


if __name__ == "__main__":
    import argparse

    default_task = (
        "Write a python script called matrix.py that generates a 3x3 random "
        "number matrix, calculates its transpose, prints both out, and execute "
        "it to make sure it runs fine."
    )
    parser = argparse.ArgumentParser(description="Run the secure coding agent.")
    parser.add_argument(
        "task",
        nargs="?",
        default=default_task,
        help="The task to give the agent (defaults to the matrix demo).",
    )
    args = parser.parse_args()
    run_secure_agent(args.task)

from .tools import AgentClientTool


LOCAL_FILES_MODULE_URL = "/-/static-plugins/datasette-agent/local-files.js"
LOCAL_FILES_TIMEOUT = 120.0


def get_local_file_client_tools():
    return [
        AgentClientTool(
            name="local_files_status",
            description=(
                "Check whether the user has selected a local folder in their "
                "browser for this chat, and summarize the selected files. Use "
                "this before attempting local file operations if you are not "
                "sure a folder is available."
            ),
            input_schema={"type": "object", "properties": {}},
            module_url=LOCAL_FILES_MODULE_URL,
            timeout=LOCAL_FILES_TIMEOUT,
        ),
        AgentClientTool(
            name="local_files_list",
            description=(
                "List files from the local folder selected by the user in their "
                "browser. Paths are relative to the selected folder; absolute "
                "local paths are never available. Use prefix to narrow to a "
                "subdirectory."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "prefix": {
                        "type": "string",
                        "description": "Optional relative path prefix to filter by",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of files to return, default 100",
                    },
                },
            },
            module_url=LOCAL_FILES_MODULE_URL,
            timeout=LOCAL_FILES_TIMEOUT,
        ),
        AgentClientTool(
            name="local_files_read_text",
            description=(
                "Read a bounded text slice from a specific file in the local "
                "folder selected by the user in their browser. Use paths "
                "returned by local_files_list or local_files_search. Prefer "
                "small max_bytes values and request additional slices only when "
                "needed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path to the file",
                    },
                    "start": {
                        "type": "integer",
                        "description": "Byte offset to start reading at, default 0",
                    },
                    "max_bytes": {
                        "type": "integer",
                        "description": "Maximum bytes to read, default 65536",
                    },
                },
                "required": ["path"],
            },
            module_url=LOCAL_FILES_MODULE_URL,
            timeout=LOCAL_FILES_TIMEOUT,
        ),
        AgentClientTool(
            name="local_files_search",
            description=(
                "Search filenames and text snippets in the local folder "
                "selected by the user in their browser. This scans likely text "
                "files with strict byte and match limits, returning relative "
                "paths and short snippets."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Case-insensitive search text",
                    },
                    "path_prefix": {
                        "type": "string",
                        "description": "Optional relative path prefix to filter by",
                    },
                    "max_matches": {
                        "type": "integer",
                        "description": "Maximum matches to return, default 20",
                    },
                    "max_bytes_per_file": {
                        "type": "integer",
                        "description": "Maximum bytes to scan per file, default 131072",
                    },
                },
                "required": ["query"],
            },
            module_url=LOCAL_FILES_MODULE_URL,
            timeout=LOCAL_FILES_TIMEOUT,
        ),
    ]

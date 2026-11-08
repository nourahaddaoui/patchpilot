"""Errors a tool raises on purpose. The message is shown to the model as-is."""


class ToolError(Exception):
    """A failure the model can act on.

    kind is a short machine label (logged, used by the grader):
    not_allowed, not_found, too_large, timeout, tool_down, bad_output, install_failed.
    """

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message

from dataclasses import dataclass


@dataclass
class UserContext:
    """Per-request runtime context, available to tools and middleware as `runtime.context`."""

    user_id: str

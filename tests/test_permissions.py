"""Permission-gate tests — the read_only/read_write modes and the per-call `writes`
classifier that lets one tool expose both read and write operations."""
from legumista_agent.permissions import Permissions
from legumista_agent.tool import Tool


def _tool(name, read_only, writes=None):
    async def run(a):  # pragma: no cover - never invoked by the gate
        return ""
    return Tool(name=name, description="", parameters={}, read_only=read_only,
                run=run, writes=writes)


def test_static_read_only_and_write_tools():
    ro, wr = _tool("r", True), _tool("w", False)
    read_only = Permissions(mode="read_only")
    read_write = Permissions(mode="read_write")
    assert read_only.check(ro, {}).allowed is True
    assert read_only.check(wr, {}).allowed is False          # write denied by default
    assert read_write.check(wr, {}).allowed is True          # allowed when opted in


def test_per_call_writes_classifier():
    """A dispatcher-style tool: reads on 'view', writes on 'sort'. read_only mode allows
    the read call and denies the write call; read_write allows both."""
    dispatch = _tool("samtools", read_only=False,
                     writes=lambda a: (a.get("args") or ["x"])[0] != "view")
    read = {"args": ["view", "-c", "f.bam"]}
    write = {"args": ["sort", "-o", "o.bam", "f.bam"]}
    ro, rw = Permissions(mode="read_only"), Permissions(mode="read_write")
    assert ro.check(dispatch, read).allowed is True
    assert ro.check(dispatch, write).allowed is False
    assert rw.check(dispatch, write).allowed is True


def test_allow_deny_rules_and_modes():
    t = _tool("t", read_only=True)
    assert Permissions(mode="read_only", deny={"t"}).check(t, {}).allowed is False
    assert Permissions(mode="deny_all").check(t, {}).allowed is False
    assert Permissions(mode="deny_all", allow={"t"}).check(t, {}).allowed is True
    assert Permissions(mode="allow_all").check(_tool("w", False), {}).allowed is True


def test_misbehaving_classifier_fails_closed():
    """If a tool's writes() raises, the call is treated as a write (denied in read_only)."""
    def boom(_a):
        raise RuntimeError("nope")
    t = _tool("t", read_only=True, writes=boom)
    assert Permissions(mode="read_only").check(t, {}).allowed is False
    assert Permissions(mode="read_write").check(t, {}).allowed is True

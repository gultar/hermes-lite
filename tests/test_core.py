"""Offline tests: tool-call parsing, think splitting, fuzzy patch, compaction split. Run: python -m pytest tests"""
from hermes_lite.compaction import split_point
from hermes_lite.llm import _ThinkSplitter, extract_inline_tool_calls, parse_arguments
from hermes_lite.tools import fuzzy

SCHEMAS = {"terminal": {"parameters": {"properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}}}}}


def test_xml_tool_call():
    text = "Let me run it.\n<tool_call>\n<function=terminal>\n<parameter=command>\nls -la\n</parameter>\n<parameter=timeout>\n30\n</parameter>\n</function>\n</tool_call>"
    content, calls = extract_inline_tool_calls(text, SCHEMAS)
    assert content == "Let me run it."
    assert calls[0].name == "terminal" and calls[0].parsed == {"command": "ls -la", "timeout": 30}


def test_json_tool_call_and_repair():
    _, calls = extract_inline_tool_calls('<tool_call>{"name": "terminal", "arguments": {"command": "pwd",}}</tool_call>', SCHEMAS)
    assert calls[0].parsed == {"command": "pwd"}
    assert parse_arguments("{'a': True}")[0] == {"a": True}
    assert parse_arguments('{"a": ')[0] is None


def test_think_splitter():
    s = _ThinkSplitter()
    r1, c1 = s.feed("<thi")
    r2, c2 = s.feed("nk>plan it</think>\nAnswer")
    r3, c3 = s.flush()
    assert (r1 + r2 + r3).strip() == "plan it" and (c1 + c2 + c3) == "Answer"


def test_fuzzy_indentation_and_uniqueness():
    src = "class A:\n    def m(self):\n        return 1\n"
    out, strategy, n = fuzzy.apply(src, "def m(self):\n    return 1", "def m(self):\n    return 2", False)
    assert "        return 2" in out and strategy == "indentation"
    try:
        fuzzy.apply("x\nx\n", "x", "y", False)
        assert False
    except ValueError as exc:
        assert "matches 2 places" in str(exc)


def test_split_point_never_starts_on_tool():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u" * 50},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "function": {"name": "t", "arguments": "{}"}}]},
            {"role": "tool", "content": "r" * 10}, {"role": "assistant", "content": "done"}]
    i = split_point(msgs, keep_tokens=5)
    assert msgs[i]["role"] != "tool"


def test_cut_off_text_tool_call_is_not_run():
    from hermes_lite.llm import extract_inline_tool_calls
    schemas = {"write_file": {"parameters": {"properties": {"path": {"type": "string"}, "content": {"type": "string"}}}}}
    full = "<tool_call>\n<function=write_file>\n<parameter=path>\na.py\n</parameter>\n<parameter=content>\nx = 1\n\n</parameter>\n</function>\n</tool_call>"
    _, calls = extract_inline_tool_calls(full, schemas, cut_off=True)
    assert calls[0].parsed == {"path": "a.py", "content": "x = 1\n"}  # the file's own newline is kept
    _, calls = extract_inline_tool_calls(full[:70], schemas, cut_off=True)
    assert calls[0].parsed is None and "cut off" in calls[0].parse_error


def test_vision_is_opt_in_on_gpu_and_respects_the_ram_budget(tmp_path, monkeypatch):
    from hermes_lite import server
    from hermes_lite.config import load_config
    model, mm = tmp_path / "model.gguf", tmp_path / "mmproj-F16.gguf"
    with open(model, "wb") as f:
        f.truncate(int(17.7 * 1024 ** 3))   # sparse: same size as the IQ4_XS model, no disk used
    with open(mm, "wb") as f:
        f.truncate(1024 ** 3)
    monkeypatch.setattr(server, "_mem_total", lambda: 23 * 1024 ** 3)
    monkeypatch.setattr(server, "_supports", lambda b, f: True)
    c = load_config()
    c["server"]["model_path"] = str(model)
    cmd = server.build_command(c)
    assert "--mmproj" not in cmd                      # off unless you ask for it
    cram = int(cmd[cmd.index("--cache-ram") + 1])
    assert cram < 512                                 # 23 GB - 17.7 GB model - 5 GB desktop: little left
    c["server"]["mmproj"] = "auto"
    cmd = server.build_command(c)
    assert cmd[cmd.index("--mmproj") + 1] == str(mm) and "--no-mmproj-offload" not in cmd   # on the GPU
    c["server"]["mmproj_offload"] = False              # asked for RAM, but RAM has no room: GPU anyway
    assert "--no-mmproj-offload" not in server.build_command(c)
    monkeypatch.setattr(server, "_mem_total", lambda: 64 * 1024 ** 3)
    assert "--no-mmproj-offload" in server.build_command(c)   # plenty of RAM: as asked


def test_saved_server_setting_reused_and_survives_command_changes(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("HERMES_LITE_HOME", str(tmp_path))
    from hermes_lite.config import load_config
    from hermes_lite.server import LlamaServer
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    c = load_config()
    srv = LlamaServer(c)
    assert srv.rung == 0 and not srv.trusted
    srv.rung = 2
    srv._save_rung()
    again = LlamaServer(load_config())
    assert again.rung == 2 and again.trusted               # reused, no retest
    # an older Hermes Lite's file (full command as signature) is still honoured by its label
    (tmp_path / "logs" / "server-tuning.json").write_text(json.dumps(
        {"signature": "llama-server -m old …", "rung": 9, "label": again.ladder[1]["label"]}))
    assert LlamaServer(load_config()).rung == 1
    # changing a memory setting in config.yaml starts the search again
    srv.rung = 2
    srv._save_rung()
    c2 = load_config()
    c2["server"]["n_cpu_moe"] = 20
    assert LlamaServer(c2).rung == 0


def test_disabled_tools_leave_the_tool_list_and_the_prompt(tmp_path, monkeypatch):
    import json, re
    monkeypatch.setenv("HERMES_LITE_HOME", str(tmp_path))
    from hermes_lite.config import load_config
    from hermes_lite.agent import Agent

    class UI:
        def event(self, *a, **k): pass
    off = ["github", "read_docs", "package_info", "process_manage"]
    c = load_config()
    c["agent"]["disabled_tools"] = off
    a = Agent(c, UI(), cwd=str(tmp_path))
    text = a.messages[0]["content"] + json.dumps(a.tools_schema())
    names = {t["function"]["name"] for t in a.tools_schema()}
    assert not names & set(off) and "terminal" in names
    for n in off:
        assert not re.search(rf"(?<![\w-]){n}(?![\w-])", text), n


def test_own_port_and_base_url_follow_server_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_LITE_HOME", str(tmp_path))
    from hermes_lite.config import load_config
    c = load_config()
    assert c["server"]["port"] == 8091 and c["model"]["base_url"] == "http://127.0.0.1:8091/v1"
    (tmp_path / "config.yaml").write_text("server:\n  port: 9000\n")
    assert load_config()["model"]["base_url"] == "http://127.0.0.1:9000/v1"

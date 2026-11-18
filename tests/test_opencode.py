"""The opencode preset against screens and session rows recorded from OpenCode 2.3.0 in a 220x50 tmux pane: busy and
idle, prompts that need a human, the input line, the context hint, launch and resume arguments, the MCP config it
reads from OPENCODE_CONFIG, and the opencode-sqlite transcript adapter."""
import json
import os
import shlex
import sqlite3
from pathlib import Path

from helpers import Sandbox

from theseus import agents as ag
from theseus import config as cfg
from theseus import gateway, profiles
from theseus.adapters import opencode

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "opencode"


def screen(name):
    return (FIXTURES / f"screen-{name}.txt").read_text().splitlines()


class OpenCodeProfileTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.scaffold()
        self.write("theseus.local.toml", '[workers.oc]\npreset = "opencode"\nmodel = "cborg/lbl/cborg-coder-ultra"\n'
                   'mcp_servers = ["theseus"]\n')
        self.conf = cfg.load(root=self.project)
        self.settings = profiles.interactive(self.conf, "oc")

    def test_busy_idle_and_prompts_that_need_a_human(self):
        for name in ("busy", "compacting", "retrying"):
            self.assertTrue(ag.pane_busy(self.conf, None, "oc", screen(name)), name)
            self.assertIsNone(ag.pane_prompt(self.conf, None, "oc", screen(name)), name)
        for name in ("idle", "home", "error"):
            self.assertFalse(ag.pane_busy(self.conf, None, "oc", screen(name)), name)
            self.assertIsNone(ag.pane_prompt(self.conf, None, "oc", screen(name)), name)
        for name in ("permission", "question"):
            self.assertFalse(ag.pane_busy(self.conf, None, "oc", screen(name)), name)
            self.assertTrue(ag.pane_prompt(self.conf, None, "oc", screen(name)), name)
        self.assertIn("Allow once", ag.pane_prompt(self.conf, None, "oc", screen("permission")))

    def test_the_input_line_ignores_the_model_row_the_placeholder_and_the_sidebar(self):
        pattern = self.settings["input_pattern"]
        for name in ("idle", "home", "busy"):
            self.assertIsNot(False, ag.input_empty(screen(name), pattern), f"{name} counts as an empty input line")
        self.assertIs(False, ag.input_empty(screen("typed"), pattern))
        self.assertTrue(ag.input_holds(screen("typed"), "theseus: hello world typed but not sent", pattern))
        self.assertIsNone(ag.input_empty(screen("home-typed"), pattern),
                          "the home screen centers its input box, above the bottom lines the checks read")
        self.assertFalse(ag.input_holds(screen("idle"), "theseus: hello world typed but not sent", pattern))
        words = " ".join(f"word{n}" for n in range(120))
        self.assertTrue(ag.input_holds(screen("wrapped"), words, pattern), "the last row of a wrapped input")

    def test_context_hint_and_gateway_error_on_screen(self):
        self.assertEqual(7000, gateway.context_tokens(self.conf, "oc", screen("idle")))
        self.assertIsNone(gateway.context_tokens(self.conf, "oc", screen("home")))
        found = gateway.detect(self.conf, "oc", screen("error"), {"text": "API Error: 503 stub 503", "error": "server_error"})
        self.assertEqual((503, "server_error", False), (found["code"], found["kind"], found["fatal"]))
        self.assertIsNone(gateway.detect(self.conf, "oc", screen("idle")))
        answer = [line.replace("\u00b7 1m 24s", "\u00b7 9.3s \u00b7 9.9 tok/s \u00b7 970ms ttft") for line in screen("error")]
        self.assertNotEqual(screen("error"), answer)
        self.assertIsNone(gateway.detect(self.conf, "oc", answer), "an answer whose text starts with Error: is no gateway error")

    def test_fresh_start_and_resume_arguments_model_and_mcp_environment(self):
        global_config = Path(os.environ["XDG_CONFIG_HOME"]) / "opencode" / "opencode.jsonc"
        global_config.parent.mkdir(parents=True, exist_ok=True)
        global_config.write_text('{\n  // the user\'s own servers\n  "mcp": {"servers": {"heavy": {"type": "local", '
                                 '"command": ["heavy"]}, "remote": {"type": "remote", "url": "https://example.invalid"},},},\n}\n')
        profile = profiles.resolve(self.conf, "oc")
        session = profiles.new_session(profile)
        self.assertTrue(session.startswith("ses_"))
        argv, env = profiles.interactive_argv(self.conf, profile, "oc-1", session)
        self.assertEqual(["opencode", "--standalone", "--session", session], argv)
        argv, _ = profiles.interactive_argv(self.conf, profile, "oc-1", session, resume=True)
        self.assertEqual(["opencode", "--standalone", "--session", session], argv)
        self.assertEqual({"model": "cborg/lbl/cborg-coder-ultra"}, json.loads(env["OPENCODE_CONFIG_CONTENT"]))
        config = json.loads(Path(env["OPENCODE_CONFIG"]).read_text())
        server = config["mcp"]["servers"]["theseus"]
        self.assertEqual({"heavy", "remote", "theseus"}, set(config["mcp"]["servers"]))
        self.assertTrue(config["mcp"]["servers"]["heavy"]["disabled"] and config["mcp"]["servers"]["remote"]["disabled"],
                        "servers of the user's own configuration that mcp_servers leaves out are disabled")
        self.assertEqual({"type": "local", "command": ["theseus", "mcp"]}, {k: server[k] for k in ("type", "command")})
        self.assertEqual(str(profiles.agent_credentials(self.conf, "oc-1")), server["environment"]["THESEUS_CREDENTIALS"])
        headless = profiles.launch_argv(profile, "do it", session, profile["model"], self.conf, "w-1",
                                        self.project / "creds")
        self.assertEqual("env", headless[0])
        self.assertTrue(headless[1].startswith("OPENCODE_CONFIG="))
        self.assertEqual(["opencode", "run", "--standalone", "--format", "json", "--session", session, "--model",
                          "cborg/lbl/cborg-coder-ultra", "do it"], headless[2:])
        launch = profiles.instance_env(self.conf, "oc", "oc-1")["THESEUS_AGENT_LAUNCH"]
        self.assertEqual("opencode --standalone", shlex.split(launch)[-2] + " " + shlex.split(launch)[-1])
        from theseus import coordinator
        argv, env = coordinator.launch(self.conf, "oc", session)
        self.assertEqual(["opencode", "--standalone", "--session", session], argv)
        server = json.loads(Path(env["OPENCODE_CONFIG"]).read_text())["mcp"]["servers"]["theseus"]
        self.assertEqual(["theseus", "mcp", "--role", "coordinator"], server["command"])
        plain = profiles.resolve(self.conf, "opencode")
        self.assertEqual({"OPENCODE_DISABLE_AUTOUPDATE": "1"}, profiles.env(plain), "no model, no model_env")
        self.assertEqual([], profiles.problems("oc", {"preset": "opencode", "mcp_format": "opencode",
                                                      "mcp_env": {"OPENCODE_CONFIG": "{mcp_config}"}}))
        self.assertTrue(profiles.problems("oc", {"mcp_format": "yaml", "model_env": {"X": 1}}))


class OpenCodeTranscriptTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.recorded = json.loads((FIXTURES / "sessions.json").read_text())
        self.db = self.tmp / "opencode.db"
        with sqlite3.connect(self.db) as conn:
            for statement in self.recorded["schema"]:
                conn.execute(statement)
            for label in self.recorded["sessions"]:
                self.add(conn, label)

    def add(self, conn, label, upto=None):
        recorded = self.recorded["sessions"][label]
        row = recorded["session"]
        conn.execute(f"INSERT OR IGNORE INTO session_v2 ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                     list(row.values()))
        for message in recorded["messages"][:upto]:
            conn.execute("INSERT OR REPLACE INTO session_message (id, session_id, type, seq, time_created, time_updated, data) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", (message["id"], row["id"], message["type"], message["seq"],
                                                          message["time_created"], message["time_updated"], message["data"]))

    def sid(self, label):
        return self.recorded["sessions"][label]["session"]["id"]

    def test_context_model_and_conversation_match_what_opencode_showed(self):
        state = opencode.read(opencode.session_path(self.db, self.sid("answer")))
        self.assertEqual((4902, "lbl/cborg-coder-ultra", "answer"), (state["tokens"], state["model"], state["last"]))
        self.assertEqual([("user", "Reply with the single word pong and nothing else."), ("assistant", "pong")],
                         [(e["kind"], e["text"]) for e in state["entries"]])
        tools = opencode.read(self.db, self.sid("tools"))
        self.assertEqual(["user", "tool", "result", "assistant", "user", "assistant"], [e["kind"] for e in tools["entries"]])
        self.assertEqual(("read", ".env"), (tools["entries"][1]["name"], tools["entries"][1]["summary"]))
        self.assertIsNone(tools["tokens"], "a completed compaction leaves the context unknown until the next answer")
        interrupted = opencode.read(self.db, self.sid("interrupted"))
        self.assertEqual(5316, interrupted["tokens"])
        self.assertEqual("The user dismissed this question", interrupted["entries"][-1]["text"])
        self.assertTrue(interrupted["entries"][-1]["error"])

    def test_a_failed_turn_is_an_error_gateway_can_classify(self):
        state = opencode.read(self.db, self.sid("failed"))
        self.assertEqual("error", state["last"])
        error = state["errors"][-1]
        typed = gateway.classify(error["text"], error["error"])
        self.assertEqual((400, False), (typed["status"], typed["retryable"]))
        self.assertTrue(state["entries"][-1]["text"].startswith("Error: /chat/completions: Invalid model name"))
        retried = opencode.read(self.db, self.sid("retried"))
        kinds = [(gateway.classify(e["text"], e["error"])["label"], e["error"]) for e in retried["errors"]]
        self.assertEqual([("503 unavailable", "server_error"), ("429 rate limit", "rate_limit")], kinds)
        self.assertEqual(["user", "assistant", "user", "assistant"], [e["kind"] for e in retried["entries"]])

    def test_reads_are_incremental_and_wait_for_a_streaming_answer(self):
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM session_message")
        recorded = self.recorded["sessions"]["answer"]["messages"]
        streaming = json.loads(recorded[1]["data"])
        streaming["time"].pop("completed")
        self.add(conn, "answer", upto=1)
        conn.execute("INSERT INTO session_message (id, session_id, type, seq, time_created, time_updated, data) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (recorded[1]["id"], self.sid("answer"), "assistant", recorded[1]["seq"],
                                                      recorded[1]["time_created"], recorded[1]["time_updated"],
                                                      json.dumps(streaming)))
        conn.commit()
        state = opencode.read(self.db, self.sid("answer"))
        self.assertEqual((1, None), (state["total"], state["tokens"]))
        self.add(conn, "answer")
        conn.commit()
        state = opencode.read(self.db, self.sid("answer"))
        self.assertEqual((2, 4902), (state["total"], state["tokens"]))
        self.assertEqual([1, 2], [e["n"] for e in state["entries"]])
        conn.close()

    def test_session_file_and_state_need_the_database_and_a_supervised_session(self):
        interactive = {"env": {"OPENCODE_DB": str(self.db)}}
        path = opencode.session_file(None, interactive, self.sid("answer"), "/anywhere")
        self.assertTrue(path.exists())
        self.assertEqual(self.sid("answer"), path.session)
        self.assertIsNone(opencode.session_file(None, {"env": {"OPENCODE_DB": str(self.tmp / "none.db")}}, "ses_x", "/"))
        agent = {"name": "a", "managed": True, "session_id": self.sid("answer")}
        self.assertEqual(4902, opencode.session_state(None, agent, interactive)["tokens"])
        self.assertIsNone(opencode.session_state(None, {**agent, "managed": False}, interactive))
        self.assertIsNone(opencode.read(self.db, "ses_missing"))

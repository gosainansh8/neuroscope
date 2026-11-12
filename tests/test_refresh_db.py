"""The refresh orchestration: steps run independently and every run is recorded. No network."""

from neuroscope import refresh


def test_each_step_runs_on_its_own_and_the_run_is_recorded(conn):
    calls = []

    def ok(name):
        def fn(c):
            calls.append(name)
            return {"done": name}
        return fn

    def broken(c):
        calls.append("fda")
        c.execute("SELECT * FROM no_such_table")          # leaves the transaction failed, like a real error
        return {}

    functions = {"trials": ok("trials"), "fda": broken, "cache": ok("cache")}
    r = refresh.run_refresh(conn, ["trials", "fda", "cache"], functions)
    assert calls == ["trials", "fda", "cache"]              # the failure did not stop the next step
    assert r["status"] == "partial"
    assert [(s["step"], s["ok"]) for s in r["steps"]] == [("trials", True), ("fda", False), ("cache", True)]
    assert "no_such_table" in r["steps"][1]["error"]
    row = conn.execute("SELECT status, finished_at IS NOT NULL, jsonb_array_length(steps) FROM refresh_runs "
                       "WHERE id = %s", (r["run_id"],)).fetchone()
    assert row == ("partial", True, 3)


def test_all_steps_failing_is_failed_and_all_passing_is_succeeded(conn):
    def boom(c):
        raise RuntimeError("source unreachable")
    assert refresh.run_refresh(conn, ["trials"], {"trials": boom})["status"] == "failed"
    assert refresh.run_refresh(conn, ["trials"], {"trials": lambda c: {}})["status"] == "succeeded"


def test_cache_step_clears_saved_answers(conn):
    conn.execute("INSERT INTO api_answer_cache (question_key, question, response) VALUES ('k', 'q', '{}')")
    conn.commit()
    assert refresh.step_cache(conn) == {"saved_answers_cleared": 1}
    assert conn.execute("SELECT COUNT(*) FROM api_answer_cache").fetchone()[0] == 0


def test_unknown_step_is_refused(capsys):
    assert refresh.main(["--steps", "trials,nonsense"]) == 2
    assert "nonsense" in capsys.readouterr().out

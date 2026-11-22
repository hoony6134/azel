"""The default approval policy (user rule 2026-11-22): approve by default; ask only to delete, deploy and the like."""
import risk
from test_gateway import gw  # noqa: F401  (fixture)


def test_lenient_rules():
    for c in ("python3 make_pdf.py", "pip3 install weasyprint", "typst compile a.typ", "git commit -m x", "cp a b",
              "npm run build", "brew install ffmpeg", "echo hi > out.txt", "curl -s https://example.com"):
        assert risk.lenient(c) == risk.SAFE, c
    for c in ("rm -rf build", "cd /tmp && rm x", "git push origin main", "vercel deploy --prod", "cat ~/.ssh/id_rsa",
              "find . -name '*.pyc' -delete", "echo hi > /etc/hosts", "sudo reboot", "docker system prune -f",
              "scp a host:/tmp", "rsync -a out/ host:/srv/", "kubectl apply -f x.yaml", "npm publish"):
        assert risk.lenient(c) == risk.RISKY, c
    assert risk.lenient("python3 clean.py", script="import shutil\nshutil.rmtree('out')") == risk.RISKY
    assert risk.lenient("python3 chart.py", script="import matplotlib\nplt.savefig('a.png')") == risk.SAFE
    assert risk.lenient("python3 -c \"import os; os.remove('x')\"") == risk.RISKY


def test_auto_mode_approves_everything_but_the_dangerous(gw):  # noqa: F811
    g, c, _ = gw
    c.put("/v1/settings", json={"approval_mode": "auto"})
    ok = c.post("/v1/approvals", json={"device": "macbook", "tool": "run_command", "command": "pip3 install rich",
                                       "cwd": "/Users/jh/dev/x"}).json()
    assert ok["status"] == "approved" and ok["auto"]
    ask = c.post("/v1/approvals", json={"device": "macbook", "tool": "run_command", "command": "rm -rf out",
                                        "cwd": "/Users/jh/dev/x"}).json()
    assert ask["status"] == "pending"
    script = c.post("/v1/approvals", json={"device": "macbook", "tool": "claude:Bash", "command": "python3 wipe.py",
                                           "cwd": "/Users/jh/dev/x", "script": "import os\nos.remove('db.sqlite')"}).json()
    assert script["status"] == "pending"  # the script deletes: asked
    c.put("/v1/settings", json={"approval_mode": "ask"})
    assert c.post("/v1/approvals", json={"device": "macbook", "tool": "run_command", "command": "pip3 install rich",
                                         "cwd": "/Users/jh/dev/x"}).json()["status"] == "pending"
    assert c.put("/v1/settings", json={"approval_mode": "never"}).status_code == 400

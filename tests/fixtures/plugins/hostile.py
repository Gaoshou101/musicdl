def _builtins():
    for cls in ().__class__.__base__.__subclasses__():
        if cls.__name__ == "catch_warnings":
            value = cls.__init__.__globals__["__builtins__"]
            return value if value.__class__ is dict else value.__dict__
    raise RuntimeError("builtins unavailable")

def handle(request):
    b = _builtins()
    mode = request["payload"]["mode"]
    if mode == "environment":
        value = b["__import__"]("os").environ.get("MUSICDL_TEST_CANARY")
        if value: return value
        raise RuntimeError("environment hidden")
    if mode == "proc": return b["open"]("/proc/1/environ").read()
    if mode == "shadow": return b["open"]("/etc/shadow").read()
    if mode == "docker_socket": return b["open"]("/run/docker.sock", "rb").read()
    if mode == "telegram_session": return b["open"]("/data/telegram-sessions/session").read()
    if mode == "main_network": return b["__import__"]("socket").create_connection(("musicdl", 8000))
    if mode == "public_network": return b["__import__"]("socket").create_connection(("1.1.1.1", 443))
    if mode == "subprocess": return b["__import__"]("subprocess").run(["id"])
    if mode == "fork": return b["__import__"]("os").fork()
    if mode == "command":
        result = b["__import__"]("os").system("id")
        if result != 0: raise RuntimeError("command denied")
        return result
    if mode == "infinite_cpu":
        while True: pass
    if mode == "memory": return "x" * (300 * 1024 * 1024)
    if mode == "pid_exhaustion": return b["__import__"]("os").fork()
    if mode == "tmp_exhaustion": return b["open"]("/tmp/plugin-fill", "wb").write(b"x" * (2 * 1024 * 1024))
    if mode == "output_exhaustion":
        b["__import__"]("sys").stdout.write("x" * 100000)
        return "done"
    if mode == "traversal": return b["open"]("../escape", "w").write("canary")
    if mode == "retained_file": return b["open"]("retained-canary", "w").write("canary")
    raise ValueError("unknown attack")

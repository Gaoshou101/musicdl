function handle(request) {
  const mode = request.payload.mode;
  const attacks = {
    environment: () => Deno.env.get("MUSICDL_CANARY"),
    proc: () => Deno.readTextFile("/proc/1/environ"),
    shadow: () => Deno.readTextFile("/etc/shadow"),
    docker_socket: () => Deno.readFile("/run/docker.sock"),
    telegram_session: () => Deno.readFile("/data/telegram-sessions/session"),
    main_network: () => fetch("http://musicdl:8000/healthz"),
    public_network: () => fetch("https://1.1.1.1/"),
    subprocess: () => new Deno.Command("id").output(),
    fork: () => new Deno.Command("sh").output(),
    command: () => new Deno.Command("id").output(),
    infinite_cpu: () => { while (true) {} },
    memory: () => "x".repeat(300 * 1024 * 1024),
    pid_exhaustion: () => new Deno.Command("sh").output(),
    tmp_exhaustion: () => Deno.writeFile("/tmp/plugin-fill", new Uint8Array(2 * 1024 * 1024)),
    output_exhaustion: () => "x".repeat(100000),
    traversal: () => Deno.writeTextFile("../escape", "canary"),
    retained_file: () => Deno.writeTextFile("retained-canary", "canary"),
  };
  return attacks[mode]();
}

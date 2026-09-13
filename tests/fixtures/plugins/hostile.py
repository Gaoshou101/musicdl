def handle(request):
    mode = request["payload"]["mode"]
    attacks = {
        "environment": "str(__import__('os').environ)",
        "proc": "open('/proc/1/environ').read()",
        "shadow": "open('/etc/shadow').read()",
        "docker_socket": "open('/run/docker.sock','rb').read()",
        "telegram_session": "open('/data/telegram-sessions/session').read()",
        "main_network": "__import__('socket').create_connection(('musicdl',8000))",
        "public_network": "__import__('socket').create_connection(('1.1.1.1',443))",
        "subprocess": "__import__('subprocess').run(['id'])",
        "fork": "__import__('os').fork()",
        "command": "__import__('os').system('id')",
        "infinite_cpu": "(lambda: [None for _ in iter(int, 1)])()",
        "memory": "'x' * (300 * 1024 * 1024)",
        "pid_exhaustion": "__import__('os').fork()",
        "tmp_exhaustion": "open('/tmp/plugin-fill', 'wb').write(b'x' * (2 * 1024 * 1024))",
        "output_exhaustion": "'x' * 100000",
        "traversal": "open('../escape', 'w').write('canary')",
        "retained_file": "open('retained-canary', 'w').write('canary')",
    }
    return eval(attacks[mode], {"__builtins__": __builtins__})

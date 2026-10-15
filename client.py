import json
import os
import socket

socket_path = f"{os.environ['XDG_RUNTIME_DIR']}/my-daemon.sock"

def send(request):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(socket_path)
        client.sendall((json.dumps(request) + "\n").encode())
        return json.loads(client.makefile().readline())

send({"command": "set", "key": "mode", "value": "work"})
print(send({"command": "get", "key": "mode"}))
# {'value': 'work'}
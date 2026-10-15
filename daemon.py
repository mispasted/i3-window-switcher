import json
import os
import socketserver

socket_path = f"{os.environ['XDG_RUNTIME_DIR']}/my-daemon.sock"
state = {}

try:
    os.unlink(socket_path)
except FileNotFoundError:
    pass


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        request = json.loads(self.rfile.readline())

        if request["command"] == "get":
            reply = {"value": state.get(request["key"])}

        elif request["command"] == "set":
            state[request["key"]] = request["value"]
            reply = {"ok": True}

        else:
            reply = {"error": "unknown command"}

        self.wfile.write((json.dumps(reply) + "\n").encode())


os.umask(0o077)  # only your user may access the socket

with socketserver.UnixStreamServer(socket_path, Handler) as server:
    server.serve_forever()
#!/usr/bin/env python3

import socket

HOST = "0.0.0.0"
PORT = 5000
BUFFER_SIZE = 1024


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((HOST, PORT))

    print(f"UDP diagnostic server listening on {HOST}:{PORT}")

    while True:
        data, address = sock.recvfrom(BUFFER_SIZE)
        message = data.decode(errors="replace")

        if message.startswith("PROBE,"):
            parts = message.split(",")

            if len(parts) >= 3:
                sequence = parts[1]
                timestamp = parts[2]

                response = f"RESPONSE,{sequence},{timestamp}"
                sock.sendto(response.encode(), address)

                print(f"Probe {sequence} response sent to {address}")


if __name__ == "__main__":
    main()

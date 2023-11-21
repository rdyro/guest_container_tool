#!/usr/bin/env python3

import json
import os
import re
from argparse import ArgumentParser, Namespace
from hashlib import sha512
from pathlib import Path
from shutil import copyfile, rmtree
from socket import gethostname
from subprocess import check_call

ROOT_DIR = Path(__file__).parent.absolute()

# the base port is a 10 digit wide default starting port for containers
# it's based on the first 8 bytes (modulo 1000) of the sha512 hash of the hostname
BASE_PORT = (
    int.from_bytes(sha512(gethostname().encode("utf-8")).digest()[:8], "little") % 1000
) * 10 + 32000


def parse_arguments():
    parser = ArgumentParser()
    parser.add_argument("-u", "--username", type=str, help="username", default="")
    parser.add_argument("-p", "--port", type=int, help="port", default=-1)
    parser.add_argument(
        "-k", "--public-key-str", type=str, help="Public RSA key as a string", default=""
    )
    parser.add_argument(
        "-c",
        "--container-image",
        type=str,
        help="Container image name (e.g., from docker hub `ubuntu:latest`)",
        default="nvcr.io/nvidia/pytorch:23.10-py3",
    )
    parser.add_argument(
        "-g", "--gpus", default="", type=str, help="Whether and what GPUs to pass to docker"
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Dry run, do not run the container.",
        default=False,
    )
    parser.add_argument(
        "-H",
        "--reverse-proxy-host",
        type=str,
        default="",
        help="(Optionally) Host to use for reverse proxy. "
        + "If complicated, use ~/.ssh/config to set up a host alias.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="",
        help="Path to a JSON config file as an alternative to command line arguments.",
    )
    parser.add_argument(
        "--extra-docker-run-args",
        type=str,
        default="",
        help="Extra arguments to pass to docker run -- when creating the persistent container.",
    )
    args = parser.parse_args()
    if args.config != "" and Path(args.config).is_file():
        config = json.loads(Path(args.config).read_text())
        # username
        if "username" in config:
            args.username = config["username"]
        # port
        if "port" in config:
            args.port = int(config["port"])
        # the public key
        if "public_key_str" in config:
            args.public_key_str = config["public_key_str"]
        if "public-key-str" in config:
            args.public_key_str = config["public-key-str"]
        # container name
        if "container_image" in config:
            args.container_image = config["container_image"]
        if "container-image" in config:
            args.container_image = config["container-image"]
        # gpus flag
        if "gpus" in config:
            args.gpus = config["gpus"]
        # dry run
        if "dry_run" in config:
            args.dry_run = config["dry_run"]
        # reverse proxy
        if "reverse_proxy_host" in config:
            args.reverse_proxy_host = config["reverse_proxy_host"]
        if "reverse-proxy-host" in config:
            args.reverse_proxy_host = config["reverse-proxy-host"]
        # extra docker args
        if "extra_docker_run_args" in config:
            args.extra_docker_run_args = config["extra_docker_run_args"]
        if "extra-docker-run-args" in config:
            args.extra_docker_run_args = config["extra-docker-run-args"]
    assert args.username != "", "Username must be specified"
    assert args.public_key_str != "", "Public key string must be specified"
    return args


def resolve_port(args: Namespace) -> bool:
    # ports = SqliteDict("users.sqlite", autocommit=True)
    all_ports = dict(
        [
            list(re.match(r"(.*?)_(\d+)$", fname).groups())[::-1]
            for fname in os.listdir(ROOT_DIR / "connections")
            if re.match(r"(.*?)_\d+$", fname) is not None
        ]
    )
    all_ports = {int(k): v for (k, v) in all_ports.items()}
    if args.port < 0:
        port_list = list(all_ports.keys()) + [BASE_PORT]
        # port_list = [int(k) for k in ports.keys()] + [default_port]
        args.port = (max(*port_list) if len(port_list) > 1 else BASE_PORT) + 1
    if args.port in all_ports:
        if all_ports[args.port] == args.username:
            print("Port already in use by this user")
            return "in_use_by_user"
        else:
            return "in_use_by_another"
    # ports[args.port] = args.username
    return "not_in_use"


####################################################################################################
####################################################################################################
####################################################################################################


def main():
    args = parse_arguments()
    port_status = resolve_port(args)
    if port_status == "in_use_by_another":
        raise ValueError("Port already in use by another user")

    # make the directory  ###########################################
    dir_key = f"{args.username}_{args.port}".replace(" ", "_")
    storage_dir = ROOT_DIR / "connections"
    if (storage_dir / dir_key).exists():
        decision = input("Directory already exists. Overwrite? [y/n] ")
        while True:
            if decision.lower() == "n":
                print("Doing nothing, exiting.")
                return
            if decision.lower() in ("y", "yes"):
                try:
                    check_call([str(storage_dir / dir_key / "stop_container.sh")])
                except Exception:
                    pass
                rmtree(storage_dir / dir_key)
                break
    os.mkdir(storage_dir / dir_key)
    copyfile(ROOT_DIR / "Dockerfile.template", storage_dir / dir_key / "Dockerfile")

    os.chdir(storage_dir / dir_key)
    Path(storage_dir / dir_key / "authorized_keys").write_text(args.public_key_str)
    gpu_spec = "" if args.gpus == "" else f"--gpus {args.gpus}"

    # ssh screen for reverse port forwarding ######################
    if args.reverse_proxy_host != "":
        Path(storage_dir / dir_key / "ssh_reverse_tunnel.sh").write_text(
            f"""#!/usr/bin/env bash
while true; do
ssh -o ExitOnForwardFailure=yes -N -R 0.0.0.0:{args.port}:localhost:{args.port} {args.reverse_proxy_host}
done
            """
        )
        check_call(["chmod", "+x", storage_dir / dir_key / "ssh_reverse_tunnel.sh"])

    # run container script ########################################
    # allows computational processes to work well with multiprocessing
    extra_memory_spec = "--ipc=host --ulimit memlock=-1 --ulimit stack=67108864"
    Path(storage_dir / dir_key / "start_container.sh").write_text(
        f"""#!/usr/bin/env bash
docker run -d {extra_memory_spec} -p {args.port}:22 {gpu_spec} {args.extra_docker_run_args} --name {dir_key} {dir_key} || docker restart {dir_key}
[[ "{args.reverse_proxy_host}" != "" ]] && ssh {args.reverse_proxy_host} 'ufw allow {args.port}/tcp'
[[ "{args.reverse_proxy_host}" != "" ]] && screen -S {dir_key}_port_forward -d -m ./ssh_reverse_tunnel.sh
echo "Container started"
        """
    )
    check_call(["chmod", "+x", storage_dir / dir_key / "start_container.sh"])

    # stop container script #######################################
    Path(storage_dir / dir_key / "stop_container.sh").write_text(
        f"""#!/usr/bin/env bash
docker stop {dir_key} || docker kill {dir_key}
[[ "{args.reverse_proxy_host}" != "" ]] && ssh {args.reverse_proxy_host} 'ufw delete allow {args.port}/tcp'
[[ "{args.reverse_proxy_host}" != "" ]] && screen -X -S {dir_key}_port_forward quit
echo "Container stopped"
        """
    )
    check_call(["chmod", "+x", storage_dir / dir_key / "stop_container.sh"])

    # build the docker container ##################################
    check_call(
        [
            "docker",
            "build",
            "--build-arg",
            f"USERNAME={args.username}",
            "--build-arg",
            f"CONTAINER_VERSION={args.container_image}",
            "-t",
            dir_key,
            ".",
        ]
    )
    if not args.dry_run:
        check_call(["./start_container.sh"])
    print()
    print()
    print(f"Created a container for user {args.username} on port {args.port}")
    print()
    print()


if __name__ == "__main__":
    main()

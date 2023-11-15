#!/usr/bin/env python3

from argparse import ArgumentParser, Namespace
from shutil import copyfile, rmtree
import json
from pathlib import Path
import os
from subprocess import check_call

from sqlitedict import SqliteDict

ROOT_DIR = Path(__file__).parent.absolute()

DEFUALT_CONFIG = {}


def parse_arguments():
    parser = ArgumentParser()
    parser.add_argument("-u", "--username", type=str, help="username", default="")
    parser.add_argument("-p", "--port", type=int, help="port", default=-1)
    parser.add_argument(
        "-k", "--public-key", type=str, help="Public RSA key as a string", default=""
    )
    parser.add_argument(
        "-c",
        "--container-name",
        type=str,
        help="Container name",
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
        if "public_key" in config:
            args.public_key = config["public_key"]
        if "public-key" in config:
            args.public_key = config["public-key"]
        # container name
        if "container_name" in config:
            args.container_name = config["container_name"]
        if "container-name" in config:
            args.container_name = config["container-name"]
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
    assert args.public_key != "", "Public key must be specified"
    return args


def resolve_port(args: Namespace) -> bool:
    ports = SqliteDict("users.sqlite", autocommit=True)
    if args.port < 0:
        default_port = 32777
        port_list = [int(k) for k in ports.keys()] + [default_port]
        args.port = (max(*port_list) if len(port_list) > 1 else default_port) + 1
    if args.port in ports:
        print("Port already in use")
        return False
    ports[args.port] = args.username
    return True


####################################################################################################
####################################################################################################
####################################################################################################


def main():
    args = parse_arguments()
    assert resolve_port(args)

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
                rmtree(storage_dir / dir_key)
                break
    os.mkdir(storage_dir / dir_key)
    copyfile(ROOT_DIR / "Dockerfile.template", storage_dir / dir_key / "Dockerfile")

    os.chdir(storage_dir / dir_key)
    Path(storage_dir / dir_key / "authorized_keys").write_text(args.public_key)
    gpu_spec = "" if args.gpus == "" else f"--gpus {args.gpus}"

    # ssh screen for reverse port forwarding ######################
    Path(storage_dir / dir_key / ".ssh_reverse_tunnel.sh").write_text(
        f"""#!/usr/bin/env bash
ssh -N -R 0.0.0.0:{args.port}:localhost:{args.port} {args.reverse_proxy_host}
        """
    )
    check_call(["chmod", "+x", storage_dir / dir_key / ".ssh_reverse_tunnel.sh"])

    # run container script ########################################
    Path(storage_dir / dir_key / "run_container.sh").write_text(
        f"""#!/usr/bin/env bash
docker run -d -p {args.port}:22 {gpu_spec} {args.extra_docker_run_args} --name {dir_key} {dir_key} || docker restart {dir_key}
[[ "{args.reverse_proxy_host}" != "" ]] && ssh {args.reverse_proxy_host} 'ufw allow {args.port}/tcp'
[[ "{args.reverse_proxy_host}" != "" ]] && screen -S {dir_key}_port_forward -d -m ./.ssh_reverse_tunnel.sh
echo "Container started"
        """
    )
    check_call(["chmod", "+x", storage_dir / dir_key / "run_container.sh"])

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
            f"CONTAINER_VERSION={args.container_name}",
            "-t",
            dir_key,
            ".",
        ]
    )
    if not args.dry_run:
        check_call(["./run_container.sh"])
    print()
    print()
    print(f"Created a container for user {args.username} on port {args.port}")
    print()
    print()


if __name__ == "__main__":
    main()

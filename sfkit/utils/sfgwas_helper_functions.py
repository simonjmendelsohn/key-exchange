import atexit
import os
import select
import shutil
import subprocess
from time import sleep
from typing import Tuple, Union

import matplotlib.pyplot as plt
import numpy as np

from sfkit.api import get_doc_ref_dict, update_firestore, website_send_file
from sfkit.utils import constants
from sfkit.utils.helper_functions import (
    condition_or_fail,
    copy_results_to_cloud_storage,
    copy_to_out_folder,
    plot_assoc,
    postprocess_assoc,
)


def get_file_paths() -> Tuple[str, str]:
    with open(os.path.join(constants.SFKIT_DIR, "data_path.txt"), "r") as f:
        geno_file_prefix = f.readline().rstrip()
        data_path = f.readline().rstrip()
    return geno_file_prefix, data_path


def use_existing_config(role: str, doc_ref_dict: dict) -> None:
    print("Using blocks with config files")
    if role != "0":
        _, data_path = get_file_paths()

        source = f"{data_path}/p{role}/for_sfgwas"
        destination = f"{constants.EXECUTABLES_PREFIX}sfgwas/for_sfgwas"
        move(source, destination)

    config = doc_ref_dict["description"].split(constants.BLOCKS_MODE)[1]

    source = f"{constants.EXECUTABLES_PREFIX}sfgwas/config/blocks/{config}"
    destination = f"{constants.EXECUTABLES_PREFIX}sfgwas/config/gwas"
    move(source, destination)


def move(source: str, destination: str) -> None:
    print(f"Moving {source} to {destination}...")
    shutil.rmtree(destination, ignore_errors=True)
    shutil.move(source, destination)


def run_sfprotocol_with_task_updates(command: str, protocol: str, demo: bool, role: str) -> None:
    env = os.environ.copy()
    if protocol == "gwas":
        env["PROTOCOL"] = "gwas"
    elif protocol == "pca":
        env["PROTOCOL"] = "pca"

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=True,
        executable="/bin/bash",
        env=env,
    )

    timeout = 86_400
    while process.poll() is None:
        rlist, _, _ = select.select([process.stdout, process.stderr], [], [], timeout)

        if not rlist:
            process.kill()
            if timeout == 86_400:
                print("WARNING: sfgwas has been stalling for 24 hours. Killing process.")
                condition_or_fail(False, f"{protocol} protocol has been stalling for 24 hours. Killing process.")
            return

        for stream in rlist:
            line = stream.readline().decode("utf-8").strip()
            print(line)
            if constants.SFKIT_PREFIX in line:
                update_firestore(f"update_firestore::task={line.split(constants.SFKIT_PREFIX)[1]}")
            elif "Output collectively decrypted and saved to" in line or (
                protocol == "pca" and f"Saved data to cache/party{role}/Qpc.txt" in line
            ):
                timeout = 30

            check_for_failure(command, protocol, process, stream, line)

    process.wait()


def check_for_failure(command: str, protocol: str, process: subprocess.Popen, stream: list, line: str) -> None:
    if (
        stream == process.stderr
        and line
        and not line.startswith("W :")
        and "[watchdog] gc finished" not in line
        and "warning:" not in line
    ):
        print(f"FAILED - {command}")
        print(f"Stderr: {line}")
        condition_or_fail(False, f"Failed {protocol} protocol")


def post_process_results(role: str, demo: bool, protocol: str) -> None:
    doc_ref_dict: dict = get_doc_ref_dict()
    user_id: str = doc_ref_dict["participants"][int(role)]

    if protocol == "gwas":
        make_new_assoc_and_manhattan_plot(doc_ref_dict, demo, role)
    elif protocol == "pca":
        make_pca_plot(role)

    if results_path := doc_ref_dict["personal_parameters"][user_id].get("RESULTS_PATH", {}).get("value", ""):
        copy_results_to_cloud_storage(role, results_path, f"{constants.EXECUTABLES_PREFIX}sfgwas/out/party{role}")

    relevant_paths = [
        f"{constants.EXECUTABLES_PREFIX}sfgwas/out/party{role}",
        f"{constants.EXECUTABLES_PREFIX}sfgwas/cache/party{role}/Qpc.txt",
        f"{constants.EXECUTABLES_PREFIX}sfgwas/stdout_party{role}.txt",
    ]
    copy_to_out_folder(relevant_paths)

    send_results: str = doc_ref_dict["personal_parameters"][user_id].get("SEND_RESULTS", {}).get("value")
    if protocol == "gwas" and send_results == "Yes":
        with open(f"{constants.EXECUTABLES_PREFIX}sfgwas/out/party{role}/new_assoc.txt", "r") as f:
            website_send_file(f, "new_assoc.txt")

        with open(f"{constants.EXECUTABLES_PREFIX}sfgwas/out/party{role}/manhattan.png", "rb") as f:
            website_send_file(f, "manhattan.png")
    elif protocol == "pca" and send_results == "Yes":
        with open(f"{constants.EXECUTABLES_PREFIX}sfgwas/cache/party{role}/Qpc.txt", "r") as f:
            website_send_file(f, "Qpc.txt")

        with open(f"{constants.EXECUTABLES_PREFIX}sfgwas/out/party{role}/pca_plot.png", "rb") as f:
            website_send_file(f, "pca_plot.png")

    update_firestore("update_firestore::status=Finished protocol!")


def make_pca_plot(role: str) -> None:
    pcs = np.loadtxt(f"{constants.EXECUTABLES_PREFIX}sfgwas/cache/party{role}/Qpc.txt", delimiter=",")
    plt.scatter(pcs[0], pcs[1])
    plt.xlabel("PC1")
    plt.ylabel("PC2")
    plt.savefig(f"{constants.EXECUTABLES_PREFIX}sfgwas/out/party{role}/pca_plot.png")


def make_new_assoc_and_manhattan_plot(doc_ref_dict: dict, demo: bool, role: str) -> None:
    # sourcery skip: assign-if-exp, introduce-default-else, swap-if-expression
    num_inds_total = 2000
    if not demo:
        num_inds_total = sum(
            int(doc_ref_dict["personal_parameters"][user]["NUM_INDS"]["value"])
            for user in doc_ref_dict["participants"]
        )
    num_covs = int(doc_ref_dict["parameters"]["num_covs"]["value"])

    snp_pos_path = f"{constants.EXECUTABLES_PREFIX}sfgwas/example_data/party{role}/snp_pos.txt"
    if not demo:
        with open(os.path.join(constants.SFKIT_DIR, "data_path.txt"), "r") as f:
            f.readline()
            data_path = f.readline().rstrip()
            snp_pos_path = f"{constants.EXECUTABLES_PREFIX}{data_path}/snp_pos.txt"

    postprocess_assoc(
        f"{constants.EXECUTABLES_PREFIX}sfgwas/out/party{role}/new_assoc.txt",
        f"{constants.EXECUTABLES_PREFIX}sfgwas/out/party{role}/assoc.txt",
        snp_pos_path,
        f"{constants.EXECUTABLES_PREFIX}sfgwas/cache/party{role}/gkeep.txt",
        "",
        num_inds_total,
        num_covs,
    )
    plot_assoc(
        f"{constants.EXECUTABLES_PREFIX}sfgwas/out/party{role}/manhattan.png",
        f"{constants.EXECUTABLES_PREFIX}sfgwas/out/party{role}/new_assoc.txt",
    )


def to_float_int_or_bool(string: str) -> Union[float, int, bool, str]:
    if string.lower() in {"true", "false"}:
        return string.lower() == "true"
    try:
        return int(string)
    except ValueError:
        try:
            return float(string)
        except ValueError:
            return string


def boot_sfkit_proxy(role: str, protocol: str) -> None:
    print("Booting up sfkit-proxy")
    doc_ref_dict: dict = get_doc_ref_dict()
    study_id: str = doc_ref_dict["study_id"]
    config_file_path = f"{constants.EXECUTABLES_PREFIX}sfgwas/config/{protocol}/configGlobal.toml"
    with open(constants.AUTH_KEY, "r") as f:
        auth_key = f.readline().rstrip()
    api_url = os.getenv("SFKIT_API_URL", "").replace("https", "wss") + "/ice"

    # do not use shell, as this may lead to security
    # vulnerabilities and improper signal handling;
    # additionally, Popen makes it run in the background,
    # instead of waiting (indefinitely) on the proxy process to complete
    command = [
        "sfkit-proxy",
        "-v",
        "-api",
        api_url,
        "-study",
        study_id,
        "-pid",
        role,
        "-mpc",
        config_file_path,
    ]
    if not auth_key.startswith("study_id:"):
        command.extend(["-auth-key", auth_key])
    print(f"Running command: {command}")
    p = subprocess.Popen(command)

    # send SIGTERM on sfkit CLI exit
    atexit.register(p.terminate)

    # 1 sec delay before we start the MPC protocol,
    # such that sfkit-proxy has time to start SOCKS listener;
    # in practice, this happens much quicker within ~100ms
    sleep(1)

    print("sfkit-proxy is running")

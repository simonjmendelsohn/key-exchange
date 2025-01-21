import fileinput
import multiprocessing
import os
import time
from shutil import copy2

from sfkit.api import get_doc_ref_dict, update_firestore, website_send_file
from sfkit.utils import constants
from sfkit.utils.helper_functions import run_command
from sfkit.utils.sfgwas_helper_functions import boot_sfkit_proxy
from sfkit.utils.sfgwas_protocol import update_config_global


def run_dti_protocol(role: str, demo: bool = False) -> None:
    print("\n\n Begin running Secure DTI protocol \n\n")
    if not demo:
        update_parameters(role)
        sync_with_other_vms(role)
        update_config_global(network_only=True)
    start_datasharing(role, demo)
    start_dti(role, demo)


def update_parameters(role: str) -> None:
    par_file = f"{constants.EXECUTABLES_PREFIX}secure-dti/mpc/par/test.par.{role}.txt"
    print(f"\n\n Updating parameters in '{par_file}'\n\n")

    doc_ref_dict = get_doc_ref_dict()

    # shared parameters and advanced parameters
    pars = {**doc_ref_dict["parameters"], **doc_ref_dict["advanced_parameters"]}

    num_cpus = str(multiprocessing.cpu_count())
    pars["NUM_THREADS"] = {"value": num_cpus}
    update_firestore(f"update_firestore::NUM_THREADS={num_cpus}")
    update_firestore(f"update_firestore::NUM_CPUS={num_cpus}")

    # update pars with ipaddresses and ports
    for i in range(len(doc_ref_dict["participants"])):
        ip = doc_ref_dict["personal_parameters"][doc_ref_dict["participants"][i]][
            "IP_ADDRESS"
        ]["value"]
        while ip == "":
            print(
                f"IP address for {doc_ref_dict['participants'][i]} is empty. Waiting..."
            )
            time.sleep(5)

            doc_ref_dict = get_doc_ref_dict()
            ip = doc_ref_dict["personal_parameters"][doc_ref_dict["participants"][i]][
                "IP_ADDRESS"
            ]["value"]

        pars[f"IP_ADDR_P{i}"] = {"value": ip}

        ports = doc_ref_dict["personal_parameters"][doc_ref_dict["participants"][i]][
            "PORTS"
        ]["value"]
        for j in range(i + 1, 4):
            pars[f"PORT_P{i}_P{j}"] = {"value": ports.split(",")[j]}

    # update file paths
    data_path = _get_data_path(role)
    pars["FEATURES_FILE"] = {"value": os.path.join(data_path, "X")}
    pars["LABELS_FILE"] = {"value": os.path.join(data_path, "y")}
    pars["TRAIN_SUFFIXES"] = {"value": os.path.join(data_path, "train_suffixes.txt")}
    pars["TEST_SUFFIXES"] = {"value": os.path.join(data_path, "test_suffixes.txt")}

    # update par file
    for line in fileinput.input(par_file, inplace=True):
        key = str(line).split(" ")[0]
        if key in pars:
            line = f"{key} " + str(pars[key]["value"]) + "\n"
        print(line, end="")


def _get_data_path(role: str) -> str:
    data_path = ''
    with open(os.path.join(constants.SFKIT_DIR, "data_path.txt"), "r") as f:
        data_path = f.readline().rstrip()
    return data_path


def sync_with_other_vms(role: str) -> None:
    update_firestore("update_firestore::status=syncing up")
    update_firestore("update_firestore::task=Syncing up machines")
    print("Begin syncing up")
    # wait until all participants have the status of starting data sharing protocol
    while True:
        doc_ref_dict: dict = get_doc_ref_dict()
        statuses = doc_ref_dict["status"].values()
        if all(status == "syncing up" for status in statuses):
            break
        print("Waiting for all participants to sync up...")
        time.sleep(5)
    print("Finished syncing up")


def _get_par_path(role: str, demo: bool) -> str:
    return f"../par/{'demo' if demo else 'test'}.par.{role}.txt"


def start_datasharing(role: str, demo: bool) -> None:
    update_firestore("update_firestore::task=Performing data sharing protocol")
    print("\n\n starting data sharing protocol \n\n")

    cwd = os.getcwd()
    command = []
    sfkit_proxy = None
    if constants.SFKIT_PROXY_ON:
        sfkit_proxy = boot_sfkit_proxy(role=role)

        proxychains_conf = os.path.join(cwd, "proxychains.conf")
        copy2("/etc/proxychains.conf", proxychains_conf)
        for line in fileinput.input(proxychains_conf, inplace=True):
            if line.startswith("socks"):
                line = f"socks5 127.0.0.1 {constants.SFKIT_PROXY_PORT}\n"
            print(line, end="")
        command += ["proxychains", "-f", proxychains_conf]

    command += ["bin/ShareData", role, _get_par_path(role, demo)]
    if role == "3":
        command.append(os.path.join(_get_data_path(role), ""))

    os.chdir(f"{constants.EXECUTABLES_PREFIX}secure-dti/mpc/code")
    run_command(command, fail_message="Failed Secure-DTI data sharing protocol")
    os.chdir(cwd)

    if sfkit_proxy:
        sfkit_proxy.terminate()

    print("Waiting for system ports to settle down...")
    time.sleep(100)

    print("\n\n Finished data sharing protocol\n\n")


def start_dti(role: str, demo: bool) -> None:
    update_firestore("update_firestore::task=Performing DTI protocol")
    print("\n\n starting DTI \n\n")

    cwd = os.getcwd()
    command = []
    if constants.SFKIT_PROXY_ON:
        boot_sfkit_proxy(role=role)

        proxychains_conf = os.path.join(cwd, "proxychains.conf")
        command += ["proxychains", "-f", proxychains_conf]

    command += ["bin/TrainSecureDTI", role, _get_par_path(role, demo)]

    if int(role) < 3:
        os.chdir(f"{constants.EXECUTABLES_PREFIX}secure-dti/mpc/code")
        run_command(command, fail_message="Failed Secure-DTI protocol")
    os.chdir(cwd)

    print("\n\n Finished DTI \n\n")

    if role == "1":
        process_output_files(role, demo)

    update_firestore("update_firestore::status=Finished protocol!")


def process_output_files(role: str, demo: bool) -> None:
    cwd = os.getcwd()
    os.chdir(f"{constants.EXECUTABLES_PREFIX}secure-dti")
    data_path = _get_data_path(role)
    run_command(
        ["python3", "bin/evaluate.py", data_path],
        fail_message="Failed to evaluate DTI results",
    )
    os.chdir(cwd)

    doc_ref_dict: dict = get_doc_ref_dict()
    user_id: str = doc_ref_dict["participants"][int(role)]

    send_results: str = (
        doc_ref_dict["personal_parameters"][user_id]
        .get("SEND_RESULTS", {})
        .get("value")
    )
    if send_results == "Yes":
        for file in ("roc_test.png", "pr_test.png"):
            with open(f"{data_path}/{file}", "rb") as f:
                website_send_file(f, file)

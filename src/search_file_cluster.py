#!/usr/bin/env python3
##########################################################################
#
#    This file is part of Proverbot9001.
#
#    Proverbot9001 is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    Proverbot9001 is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with Proverbot9001.  If not, see <https://www.gnu.org/licenses/>.
#
#    Copyright 2019 Alex Sanchez-Stern and Yousef Alhessi
#
##########################################################################

import argparse
import os
import time
import sys
import json
import subprocess
import signal
from tqdm import tqdm
from pathlib_revised import Path2

from typing import List, NamedTuple

from search_file import (add_args_to_parser, ReportJob,
                         generate_report, get_predictor,
                         get_already_done_jobs,
                         project_dicts_from_args)
import coq_serapy
import util

details_css = "details.css"
details_javascript = "search-details.js"

def main(arg_list: List[str]) -> None:
    arg_parser = argparse.ArgumentParser()

    add_args_to_parser(arg_parser)
    arg_parser.add_argument("--num-workers", default=32, type=int)
    arg_parser.add_argument("--workers-output-dir", default=Path2("output"),
                            type=Path2)
    arg_parser.add_argument("--worker-timeout", default="6:00:00")
    arg_parser.add_argument("-p", "--partition", default="defq")
    arg_parser.add_argument("--mem", default="2G")

    args = arg_parser.parse_args(arg_list)
    if args.filenames[0].suffix == ".json":
        assert args.splits_file == None
        assert len(args.filenames) == 1
        args.splits_file = args.filenames[0]
        args.filenames = []
    predictor = get_predictor(arg_parser, args)
    base = Path2(os.path.dirname(os.path.abspath(__file__)))

    if not args.output_dir.exists():
        args.output_dir.makedirs()
    if args.splits_file:
        with args.splits_file.open('r') as splits_f:
            project_dicts = json.loads(splits_f.read())
        for project_dict in project_dicts:
            (args.output_dir / project_dict["project_name"]).makedirs(exist_ok=True)
            for filename in [details_css, details_javascript]:
                destpath = args.output_dir / project_dict["project_name"] / filename
                if not destpath.exists():
                    srcpath = base.parent / 'reports' / filename
                    srcpath.copyfile(destpath)
    else:
        for filename in [details_css, details_javascript]:
            destpath = args.output_dir / filename
            if not destpath.exists():
                srcpath = base.parent / 'reports' / filename
                srcpath.copyfile(destpath)

    if args.resume:
        solved_jobs = get_already_done_jobs(args)
    else:
        remove_alrady_done_jobs(args)
        solved_jobs = []
    os.makedirs(str(args.output_dir / args.workers_output_dir), exist_ok=True)
    get_all_jobs_cluster(args)
    with open(args.output_dir / "jobs.txt") as f:
        jobs = [json.loads(line) for line in f]
        assert len(jobs) > 0
    if len(solved_jobs) < len(jobs):
        setup_jobsstate(args.output_dir, jobs, solved_jobs)
        dispatch_workers(args, arg_list)
        with util.sighandler_context(signal.SIGINT, cancel_workers):
            show_progress(args)
    else:
        assert len(solved_jobs) == len(jobs)
    generate_report(args, predictor)

def get_all_jobs_cluster(args: argparse.Namespace) -> None:
    project_dicts = project_dicts_from_args(args)
    projfiles = [(project_dict["project_name"], filename)
                 for project_dict in project_dicts
                 for filename in project_dict["test_files"]]
    with (args.output_dir / "jobs.txt").open("w") as f:
        pass
    with (args.output_dir / "proj_files.txt").open("w") as f:
        for projfile in projfiles:
            print(json.dumps(projfile), file=f)
    with (args.output_dir / "proj_files_taken.txt").open("w") as f:
        pass
    with (args.output_dir / "proj_files_scanned.txt").open("w") as f:
        pass
    worker_args = [f"--prelude={args.prelude}",
                   f"--output={args.output_dir}",
                   "proj_files.txt",
                   "proj_files_taken.txt",
                   "proj_files_scanned.txt",
                   "jobs.txt"]
    if args.include_proof_relevant:
        worker_args.append("--include-proof-relevant")
    if args.proof:
        worker_args.append(f"--proof={args.proof}")
    elif args.proofs_file:
        worker_args.append(f"--proofs-file={str(args.proofs_file)}")
    cur_dir = os.path.realpath(os.path.dirname(__file__))
    subprocess.run([f"{cur_dir}/sbatch-retry.sh",
                    "-o", str(args.output_dir / args.workers_output_dir /
                              "file-scanner-%a.out"),
                    f"--array=0-{args.num_workers-1}",
                    f"{cur_dir}/job_getting_worker.sh"] + worker_args)

    with tqdm(desc="Getting jobs", total=len(projfiles), dynamic_ncols=True) as bar:
        num_files_scanned = 0
        while num_files_scanned < len(projfiles):
            with (args.output_dir / "proj_files_scanned.txt").open('r') as f:
                new_files_scanned = len([line for line in f])
            bar.update(new_files_scanned - num_files_scanned)
            num_files_scanned = new_files_scanned
            time.sleep(0.2)


def setup_jobsstate(output_dir: Path2, all_jobs: List[ReportJob],
                    solved_jobs: List[ReportJob]) -> None:
    with (output_dir / "taken.txt").open("w") as f:
        pass
    with (output_dir / "jobs.txt").open("w") as f:
        for job in all_jobs:
            if job not in solved_jobs:
                print(json.dumps(job), file=f)
        print("", end="", flush=True, file=f)
def dispatch_workers(args: argparse.Namespace, rest_args: List[str]) -> None:
    with (args.output_dir / "num_workers_dispatched.txt").open("w") as f:
        print(args.num_workers, file=f)
    with (args.output_dir / "workers_scheduled.txt").open("w") as f:
        pass
    cur_dir = os.path.realpath(os.path.dirname(__file__))
    # If you have a different cluster management software, that still allows
    # dispatching jobs through the command line and uses a shared filesystem,
    # modify this line.
    subprocess.run([f"{cur_dir}/sbatch-retry.sh",
                    "-J", "proverbot9001-worker",
                    "-p", args.partition,
                    "-t", str(args.worker_timeout),
                    "--cpus-per-task", str(args.num_threads),
                    "-o", str(args.output_dir / args.workers_output_dir
                              / "worker-%a.out"),
                    "--mem", args.mem,
                    f"--array=0-{args.num_workers-1}",
                    f"{cur_dir}/search_file_cluster_worker.sh"] + rest_args)

def cancel_workers(*args) -> None:
    subprocess.run(["scancel -u $USER -n proverbot9001-worker"], shell=True)
    sys.exit()

def get_jobs_done(args: argparse.Namespace) -> int:
    if args.splits_file:
        return len(get_already_done_jobs(args))
    else:
        jobs_done = 0
        for filename in args.filenames:
            with (args.output_dir /
                  (util.safe_abbrev(filename, args.filenames) + "-proofs.txt")).open('r') as f:
                jobs_done += len([line for line in f])
        return jobs_done

def show_progress(args: argparse.Namespace) -> None:
    with (args.output_dir / "jobs.txt").open('r') as f:
        num_jobs_total = len([line for line in f])
    num_jobs_done = get_jobs_done(args)
    with (args.output_dir / "num_workers_dispatched.txt").open('r') as f:
        num_workers_total = int(f.read())
    with (args.output_dir / "workers_scheduled.txt").open('r') as f:
        num_workers_scheduled = len([line for line in f])

    with tqdm(desc="Jobs finished", total=num_jobs_total,
              initial=num_jobs_done, dynamic_ncols=True) as bar, \
         tqdm(desc="Workers scheduled", total=num_workers_total,
              initial=num_workers_scheduled, dynamic_ncols=True) as wbar:
        while num_jobs_done < num_jobs_total:
            new_jobs_done = get_jobs_done(args)
            bar.update(new_jobs_done - num_jobs_done)
            num_jobs_done = new_jobs_done

            with (args.output_dir / "workers_scheduled.txt").open('r') as f:
                new_workers_scheduled = len([line for line in f])
            wbar.update(new_workers_scheduled - num_workers_scheduled)
            num_workers_scheduled = new_workers_scheduled

            time.sleep(0.2)

if __name__ == "__main__":
    main(sys.argv[1:])
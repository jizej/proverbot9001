#!/usr/bin/env python3

import argparse
import subprocess
import os
import sys
import threading
import queue
import re
import datetime

from shutil import *
from format import format_context
from yattag import Doc

import serapi_instance
import linearize_semicolons
from serapi_instance import ParseError, LexError

from helper import *
from syntax import syntax_highlight
from helper import load_commands_preserve

finished_queue = queue.Queue()
rows = queue.Queue()
base = os.path.dirname(os.path.abspath(__file__))
darknet_command = ""

details_css = ["details.css", "jquery-ui.css"]
details_javascript = ["http://code.jquery.com/jquery-latest.min.js", "jquery-ui.js"]

def header(tag, doc, text, css, javascript, title):
    with tag('head'):
        for filename in css:
            doc.stag('link', href=filename, rel='stylesheet')
        for filename in javascript:
            with tag('script', type='text/javascript',
                     src=filename):
                pass
        with tag('title'):
            text(title)

def details_header(tag, doc, text, filename):
    header(tag, doc, text, details_css, details_javascript,
           "Proverbot Detailed Report for {}".format(filename))

def report_header(tag, doc, text):
    header(tag, doc, text, ["report.css"], [],
           "Proverbot Report")

def stringified_percent(total, outof):
    if outof == 0:
        return "NaN"
    else:
        return "{:10.2f}".format(total * 100 / outof)

def jsan(string):
    return (string
            .replace("\\", "\\\\")
            .replace("\n", "\\n"))

def shorten_whitespace(string):
    return string.replace("    ", "  ")

def hover_script(context_idx, proof_context, predicted_tactic):
    return ("<script type='text/javascript'>\n"
            "$(function () {{\n"
            "  $(\"#context-{}\").tooltip({{\n"
            "    content: \"<pre><code>{}\\n\\n"
            "               <b>Predicted</b>: {}\\n"
            "               </code></pre>\",\n"
            "    open: function (event, ui) {{\n"
            "      ui.tooltip.css('max-width', 'none');\n"
            "      ui.tooltip.css('min-width', '800px');\n"
            "    }}\n"
            "  }});\n"
            "}});\n"
            "</script>".format(str(context_idx),
                               shorten_whitespace(jsan(proof_context)),
                               shorten_whitespace(jsan(predicted_tactic))))

class GlobalResult:
    def __init__(self):
        self.num_tactics = 0
        self.num_correct = 0
        self.num_partial = 0
        self.num_failed = 0
        self.lock = threading.Lock()
        pass
    def add_file_result(self, result):
        self.lock.acquire()
        self.num_tactics += result.num_tactics
        self.num_correct += result.num_correct
        self.num_partial += result.num_partial
        self.num_failed += result.num_failed
        self.lock.release()
        pass
    def report_results(self, doc, text, tag, line):
        with tag('h2'):
            text("Overall Accuracy: {}% ({}/{})"
                 .format(stringified_percent(self.num_correct, self.num_tactics),
                         self.num_correct, self.num_tactics))
        with tag('table'):
            with tag('tr'):
                line('th', 'Filename')
                line('th', 'Number of Tactics in File')
                line('th', 'Number of Tactics Correctly Predicted')
                line('th', 'Number of Tactics Predicted Partially Correct')
                line('th', '% Correct')
                line('th', '% Partial')
                line('th', 'Details')
            while not rows.empty():
                fresult = rows.get()
                with tag('tr'):
                    line('td', fresult.filename)
                    line('td', fresult.num_tactics)
                    line('td', fresult.num_correct)
                    line('td', fresult.num_partial)
                    line('td', stringified_percent(fresult.num_correct,
                                                 fresult.num_tactics))
                    line('td', stringified_percent(fresult.num_partial,
                                                 fresult.num_tactics))
                    with tag('td'):
                        with tag('a', href=fresult.details_filename()):
                            text("Details")
    pass

class FileResult:
    def __init__(self, filename):
        self.num_tactics = 0
        self.num_correct = 0
        self.num_partial = 0
        self.num_failed = 0
        self.filename = filename
        pass
    def add_command_result(self, predicted, actual, exception):
        self.num_tactics += 1
        if actual.strip() == predicted.strip():
            self.num_correct += 1
            self.num_partial += 1
            return "goodcommand"
        elif (actual.strip().split(" ")[0].strip(".") ==
              predicted.strip().split(" ")[0].strip(".")):
            self.num_partial += 1
            return "okaycommand"
        elif exception == None:
            return "badcommand"
        elif type(exception) == ParseError or type(exception) == LexError:
            self.num_failed += 1
            return "superfailedcommand"
        else:
            self.num_failed += 1
            return "failedcommand"
        pass
    def details_filename(self):
        return "{}.html".format(escape_filename(self.filename))
    pass

gresult = GlobalResult()

class Worker(threading.Thread):
    def __init__(self, workerid, coqargs, includes,
                 output_dir, prelude, debug, num_jobs):
        threading.Thread.__init__(self, daemon=True)
        self.coqargs = coqargs
        self.includes = includes
        self.workerid = workerid
        self.output_dir = output_dir
        self.prelude = prelude
        self.debug = debug
        self.num_jobs = num_jobs
        pass

    def get_commands(self, filename):
        return lift_and_linearize(load_commands_preserve(self.prelude + "/" + filename),
                                  self.coqargs, self.includes, self.prelude,
                                  filename, debug=self.debug)

    def process_file(self, filename):
        global gresult
        fresult = FileResult(filename)
        current_context = 0
        scripts = ""

        if self.debug:
            print("Preprocessing...")
        commands = self.get_commands(filename)

        doc, tag, text, line = Doc().ttl()

        with tag('html'):
            details_header(tag, doc, text, filename)
            with serapi_instance.SerapiContext(self.coqargs,
                                               self.includes,
                                               self.prelude) as coq:
                with tag('body'), tag('pre'):
                    for command in commands:
                        if re.match(";", command) and options["no-semis"]:
                            coq.run_stmt(command)
                            return
                        in_proof = (coq.proof_context and
                                    not re.match(".*Proof.*", command.strip()))
                        if in_proof:
                            query = format_context(coq.prev_tactics, coq.get_hypothesis(),
                                                   coq.get_goals())
                            response, errors = subprocess.Popen(darknet_command,
                                                                stdin=
                                                                subprocess.PIPE,
                                                                stdout=
                                                                subprocess.PIPE,
                                                                stderr=
                                                                subprocess.PIPE
                            ).communicate(input=query.encode('utf-8'))
                            result = response.decode('utf-8', 'ignore').strip()
                            assert("." in result)

                            scripts += hover_script(current_context,
                                                    coq.proof_context,
                                                    result)
                            exception = None
                            try:
                                coq.quiet = True
                                coq.run_stmt(result)
                                coq.quiet = False
                                coq.cancel_last()
                            except (ParseError, LexError) as e:
                                exception = e
                                coq.get_completed()
                            except (CoqExn, BadResponse) as e:
                                exception = e
                                coq.cancel_last()

                            with tag('span', title='tooltip',
                                     id='context-' + str(current_context)):
                                grade = fresult.add_command_result(result, command,
                                                                   exception)
                                with tag('code', klass=grade):
                                    text(command)
                                current_context += 1
                        else:
                            with tag('code', klass="plaincommand"):
                                text(command)

                        try:
                            coq.run_stmt(command)
                        except (AckError, CompletedError, CoqExn,
                                BadResponse, ParseError, LexError):
                            print("In file {}:".format(filename))
                            raise

        with open("{}/{}".format(self.output_dir, fresult.details_filename), "w") as fout:
            fout.write(syntax_highlight(doc.getvalue()) + scripts)

        gresult.add_file_result(fresult)
        rows.put(fresult)
    def run(self):
        try:
            while(True):
                job = jobs.get_nowait()
                jobnum = num_jobs - jobs.qsize()
                print("Processing file {} ({} of {})".format(job,
                                                             jobnum,
                                                             num_jobs))
                self.process_file(job)
                print("Finished file {} ({} of {})".format(job,
                                                           jobnum,
                                                           num_jobs))
        except queue.Empty:
            pass
        finally:
            finished_queue.put(self.workerid)

def escape_filename(filename):
    return re.sub("/", "Zs", re.sub("\.", "Zd", re.sub("Z", "ZZ", filename)))

parser = argparse.ArgumentParser(description=
                                 "try to match the file by predicting a tacti")
parser.add_argument('-j', '--threads', default=1, type=int)
parser.add_argument('--prelude', default=".")
parser.add_argument('--debug', default=False, const=True, action='store_const')
parser.add_argument('-o', '--output', help="output data folder name",
                    default="report")
parser.add_argument('-p', '--predictor',
                    help="The command to use to predict tactics. This command must "
                    "accept input on standard in in the format specified in format.py, "
                    "and produce a tactic on standard out. The first \"{}\" in the "
                    "command will be replaced with the base directory.",
                    default="{}/try-auto.py")
parser.add_argument('filenames', nargs="+", help="proof file name (*.v)")
args = parser.parse_args()
darknet_command = [args.predictor.format(base)]

coqargs = ["{}/coq-serapi/sertop.native".format(base),
           "--prelude={}/coq".format(base)]
includes = subprocess.Popen(['make', '-C', args.prelude, 'print-includes'],
                            stdout=subprocess.PIPE).communicate()[0].decode('utf-8')

# Get some metadata
cur_commit = subprocess.check_output(["git show --oneline | head -n 1"],
                                     shell=True).decode('utf-8')
cur_date = datetime.datetime.now()

if not os.path.exists(args.output):
    os.makedirs(args.output)

jobs = queue.Queue()
workers = []
num_jobs = len(args.filenames)
for infname in args.filenames:
    jobs.put(infname)

for idx in range(args.threads):
    worker = Worker(idx, coqargs, includes, args.output,
                    args.prelude, args.debug, num_jobs)
    worker.start()
    workers.append(worker)

for idx in range(args.threads):
    finished_id = finished_queue.get()
    workers[finished_id].join()
    print("Thread {} finished ({} of {}).".format(finished_id, idx + 1, args.threads))

###
### Write the report page out
###

doc, tag, text, line = Doc().ttl()

with tag('html'):
    report_header(tag, doc, text)
    with tag('body'):
        with tag('h4'):
            text("Using predictor: {}".format(darknet_command[0]))
        with tag('h4'):
            text("{} files processed".format(num_jobs))
        with tag('h5'):
            text("Commit: {}".format(cur_commit))
        with tag('h5'):
            text("Run on {}".format(cur_date))
        gresult.report_results(doc, text, tag, line)

extra_files = ["report.css", "details.css", "jquery-ui.css", "jquery-ui.js"]

for filename in extra_files:
    copy(base + "/" + filename, args.output + "/" + filename)

with open("{}/report.html".format(args.output), "w") as fout:
    fout.write(doc.getvalue())

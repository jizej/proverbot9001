#!/usr/bin/env python3

import argparse
import subprocess
import os
import sys
import multiprocessing
import re
import datetime
import time
import csv
import collections
import itertools
import functools
import shutil
import io
import math

from typing import Any, Union, Optional, Tuple, List, Sequence, Dict, Counter, \
    Callable, NamedTuple, TextIO, Iterable, \
    cast, TypeVar, NewType

from data import file_chunks, filter_data
from context_filter import get_context_filter
from serapi_instance import get_stem
from predict_tactic import predictors, loadPredictor
from models.tactic_predictor import TacticPredictor, Prediction, TacticContext
from yattag import Doc
from format import format_goal, format_hypothesis, format_tactic, read_tuple, \
    ScrapedTactic, ScrapedCommand
from helper import try_load_lin, load_commands_preserve
from syntax import syntax_highlight, strip_comments
from util import multipartition, chunks

Tag = Callable[..., Doc.Tag]
Text = Callable[..., None]
Line = Callable[..., None]

MixedDataset = Iterable[ScrapedCommand]

details_css = ["details.css"]
details_javascript = ["details.js"]
report_css = ["report.css"]
report_js = ["report.js"]
extra_files = details_css + details_javascript + report_css + report_js + ["logo.png"]

predictor : TacticPredictor

def read_text_data2_worker__(lines : List[str]) -> MixedDataset:
    def worker_generator():
        with io.StringIO("".join(lines)) as f:
            t = read_tuple(f)
            while t:
                yield t
                t = read_tuple(f)
    return list(worker_generator())

def read_text_data_singlethreaded(data_path : str,
                                  num_threads:Optional[int]=None) -> MixedDataset:
    line_chunks = file_chunks(data_path, 32768)
    yield from itertools.chain.from_iterable((read_text_data2_worker__(chunk) for chunk in line_chunks))

def to_list_string(l : List[Any]) -> str:
    return "% ".join([str(item) for item in l])

def stringified_percent(total : float, outof : float) -> str:
    if outof == 0:
        return "NaN"
    else:
        return "{:10.2f}".format(total * 100 / outof)

def escape_filename(filename : str) -> str:
    return re.sub("/", "Zs", re.sub("\.", "Zd", re.sub("Z", "ZZ", filename)))

class PredictionResult(NamedTuple):
    prediction : str
    grade : str
    certainty : float

class TacticResult(NamedTuple):
    tactic : str
    hypothesis : List[str]
    goal : str
    prediction_results : List[PredictionResult]

CommandResult = Union[Tuple[str], TacticResult]

def main(arg_list : List[str]) -> None:
    global predictor
    parser = argparse.ArgumentParser(description=
                                     "Produce an html report from the scrape file.")
    parser.add_argument("-j", "--threads", default=1, type=int)
    parser.add_argument("--prelude", default=".")
    parser.add_argument("--debug", default=False, const=True, action='store_const')
    parser.add_argument("--output", "-o", help="output data folder name",
                        default="report")
    parser.add_argument("--message", "-m", default=None)
    parser.add_argument('--context-filter', dest="context_filter", type=str,
                        default=None)
    parser.add_argument('--weightsfile', default="data/pytorch-weights.tar")
    parser.add_argument('--predictor', choices=list(predictors.keys()), default=list(predictors.keys())[0])
    parser.add_argument("--num-predictions", dest="num_predictions", type=int, default=3)
    parser.add_argument('--skip-nochange-tac', default=False, const=True, action='store_const',
                        dest='skip_nochange_tac')
    parser.add_argument('filenames', nargs="+", help="proof file name (*.v)")
    args = parser.parse_args(arg_list)

    cur_commit = subprocess.check_output(["git show --oneline | head -n 1"],
                                         shell=True).decode('utf-8').strip()
    cur_date = datetime.datetime.now()
    predictor = loadPredictor(args.weightsfile, args.predictor)

    if not os.path.exists(args.output):
        os.makedirs(args.output)

    context_filter = args.context_filter or dict(predictor.getOptions())["context_filter"]

    with multiprocessing.pool.ThreadPool(args.threads) as pool:
        file_results = \
            list((stats for stats in
                  pool.imap_unordered(functools.partial(report_file, args, context_filter),
                                      args.filenames)
                  if stats))

    write_summary(args, predictor.getOptions() +
                  [("report type", "static"), ("predictor", args.predictor)],
                  cur_commit, cur_date, file_results)

T1 = TypeVar('T1')
T2 = TypeVar('T2')

def report_file(args : argparse.Namespace,
                context_filter_str : str,
                filename : str) -> Optional['ResultStats']:

    def make_predictions(num_predictions : int,
                         tactic_interactions : List[ScrapedTactic]) -> \
        Tuple[Iterable[Tuple[ScrapedTactic, List[Prediction]]], float]:
        if len(tactic_interactions) == 0:
            return [], 0
        chunk_size = 4096
        total_loss = 0.
        for tactic_interaction in tactic_interactions:
            assert isinstance(tactic_interaction.goal, str)
        inputs = [TacticContext(tactic_interaction.prev_tactics,
                                tactic_interaction.hypotheses,
                                format_goal(tactic_interaction.goal))
                  for tactic_interaction in tactic_interactions]
        corrects = [tactic_interaction.tactic
                    for tactic_interaction in tactic_interactions]
        predictions : List[List[Prediction]] = []
        for inputs_chunk, corrects_chunk in zip(chunks(inputs, chunk_size),
                                                chunks(corrects, chunk_size)):
            predictions_chunk, loss = predictor.predictKTacticsWithLoss_batch(
                inputs_chunk, args.num_predictions, corrects_chunk)
            predictions += predictions_chunk
            total_loss += loss
        del inputs
        del corrects
        return list(zip(tactic_interactions, predictions)), \
            total_loss / math.ceil(len(tactic_interactions) / chunk_size)

    def merge_indexed(lic : Sequence[Tuple[int, T1]], lib : Sequence[Tuple[int,T2]]) \
        -> Iterable[Union[T1, T2]]:
        lic = list(reversed(lic))
        lib = list(reversed(lib))
        while lic and lib:
            lst : List[Tuple[int, Any]] = (lic if lic[-1][0] < lib[-1][0] else lib) # type: ignore
            yield lst.pop()[1]
        yield from list(reversed([c for _, c in lic]))
        yield from list(reversed([b for _, b in lib]))
    def get_should_filter(data : MixedDataset) -> Iterable[Tuple[ScrapedCommand, bool]]:
        list_data : List[ScrapedCommand] = list(data)
        extended_list : List[Optional[ScrapedCommand]] = \
            cast(List[Optional[ScrapedCommand]], list_data[1:])  + [None]
        for point, nextpoint in zip(list_data, extended_list):
            if isinstance(point, ScrapedTactic):
                if isinstance(nextpoint, ScrapedTactic):
                    yield(point, not context_filter({"goal":format_goal(point.goal),
                                                     "hyps":point.hypotheses},
                                                    point.tactic,
                                                    {"goal":format_goal(nextpoint.goal),
                                                     "hyps":nextpoint.hypotheses}))
                else:
                    yield(point, not context_filter({"goal":format_goal(point.goal),
                                                     "hyps":point.hypotheses},
                                                    point.tactic,
                                                    {"goal":"",
                                                     "hyps":""}))
            else:
                yield (point, True)
    try:
        scrape_path = args.prelude + "/" + filename + ".scrape"
        interactions = list(read_text_data_singlethreaded(scrape_path))
        print("Loaded {} interactions for file {}".format(len(interactions), filename))
    except FileNotFoundError:
        print("Couldn't find file {}, skipping...".format(scrape_path))
        return None
    context_filter = get_context_filter(context_filter_str)

    command_results : List[CommandResult] = []
    stats = ResultStats(filename)
    indexed_filter_aware_interactions = list(enumerate(get_should_filter(interactions)))
    for idx, (interaction, should_filter) in indexed_filter_aware_interactions:
        assert isinstance(idx, int)
        if not should_filter:
            assert isinstance(interaction, ScrapedTactic), interaction
    indexed_filter_aware_prediction_contexts, indexed_filter_aware_pass_through = \
        multipartition(indexed_filter_aware_interactions,
                       lambda indexed_filter_aware_interaction:
                       indexed_filter_aware_interaction[1][1])
    indexed_prediction_contexts: List[Tuple[int, ScrapedTactic]] = \
        [(idx, cast(ScrapedTactic, obj)) for (idx, (obj, filtered))
         in indexed_filter_aware_prediction_contexts]
    indexed_pass_through = [(idx, cast(Union[ScrapedTactic, str], obj))
                            for (idx, (obj, filtered))
                            in indexed_filter_aware_pass_through]
    for idx, prediction_context in indexed_prediction_contexts:
        assert isinstance(idx, int)
        assert isinstance(prediction_context, ScrapedTactic)
    prediction_interactions, loss = \
        make_predictions(args.num_predictions,
                         [prediction_context for idx, prediction_context
                          in indexed_prediction_contexts])
    indexed_prediction_interactions = \
        [(idx, prediction_interaction)
         for (idx, prediction_context), prediction_interaction
         in zip(indexed_prediction_contexts, prediction_interactions)]
    interactions_with_predictions = \
        list(merge_indexed(indexed_prediction_interactions, indexed_pass_through))

    for inter in interactions_with_predictions:
        if isinstance(inter, tuple) and not isinstance(inter, ScrapedTactic):
            assert len(inter) == 2, inter
            (prev_tactics, hyps, goal, correct_tactic), \
                predictions_and_certainties \
                = inter #cast(Tuple[ScrapedTactic, List[Prediction]], inter)
            prediction_results = [PredictionResult(prediction,
                                                   grade_prediction(correct_tactic,
                                                                    prediction),
                                                   certainty)
                                  for prediction, certainty in
                                  predictions_and_certainties]
            command_results.append(TacticResult(correct_tactic, hyps, goal,
                                                prediction_results))
            stats.add_tactic(prediction_results,
                             correct_tactic)
        elif isinstance(inter, ScrapedTactic):
            command_results.append((inter.tactic,))
        else:
            command_results.append((inter,))

    stats.set_loss(loss)

    print("Finished grading file {}".format(filename))

    write_html(args.output, filename, command_results, stats)
    write_csv(args.output, filename, args, command_results, stats)
    print("Finished output for file {}".format(filename))
    return stats

proper_subs = {"auto.": "eauto."}

def grade_prediction(correct_tactic : str, prediction : str):
    if correct_tactic.strip() == prediction.strip():
        return "goodcommand"
    elif get_stem(correct_tactic) == get_stem(prediction):
        return "okaycommand"
    elif correct_tactic.strip() in proper_subs and \
         proper_subs[correct_tactic.strip()] == prediction.strip():
        return "mostlygoodcommand"
    else:
        return "badcommand"

###
### Write the report page out
###
def write_summary(args : argparse.Namespace, options : Sequence[Tuple[str, str]],
                  cur_commit : str, cur_date : datetime.datetime,
                  individual_stats : List['ResultStats']) -> None:
    def report_header(tag : Any, doc : Doc, text : Text) -> None:
        header(tag, doc, text,report_css, report_js,
               "Proverbot Report")
    combined_stats = combine_file_results(individual_stats)
    doc, tag, text, line = Doc().ttl()
    with tag('html'):
        report_header(tag, doc, text)
        with tag('body'):
            with tag('h4'):
                text("{} files processed".format(len(args.filenames)))
            with tag('h5'):
                text("Commit: {}".format(cur_commit))
            if args.message:
                with tag('h5'):
                    text("Message: {}".format(args.message))
            with tag('h5'):
                text("Run on {}".format(cur_date.strftime("%Y-%m-%d %H:%M:%S.%f")))
            with tag('img',
                     ('src', 'logo.png'),
                     ('id', 'logo')):
                pass
            with tag('h2'):
                text("Overall Accuracy: {}% ({}/{})"
                     .format(stringified_percent(combined_stats.num_correct,
                                                 combined_stats.num_tactics),
                             combined_stats.num_correct, combined_stats.num_tactics))
            with tag('ul'):
                for k, v in options:
                    if k == 'filenames':
                        continue
                    elif k == 'message':
                        continue
                    elif not v:
                        continue
                    with tag('li'):
                        text("{}: {}".format(k, v))
            with tag('table'):
                with tag('tr', klass="header"):
                    line('th', 'Filename')
                    line('th', 'Number of Tactics in File')
                    line('th', '% Initially Correct')
                    line('th', '% Top {}'.format(args.num_predictions))
                    line('th', '% Partial')
                    line('th', '% Top {} Partial'.format(args.num_predictions))
                    line('th', 'Testing Loss')
                    line('th', 'Details')
                sorted_rows = sorted(individual_stats,
                                     key=lambda fresult: fresult.num_tactics,
                                     reverse=True)

                for fresult in sorted_rows:
                    if fresult.num_tactics == 0:
                        continue
                    with tag('tr'):
                        line('td', fresult.filename)
                        line('td', str(fresult.num_tactics))
                        line('td', stringified_percent(fresult.num_correct,
                                                       fresult.num_tactics))
                        line('td', stringified_percent(fresult.num_topN,
                                                       fresult.num_tactics))
                        line('td', stringified_percent(fresult.num_partial,
                                                       fresult.num_tactics))
                        line('td', stringified_percent(fresult.num_topNPartial,
                                                       fresult.num_tactics))
                        line('td', "{:10.2f}".format(fresult.loss))
                        with tag('td'):
                            with tag('a', href=escape_filename(fresult.filename) + ".html"):
                                text("Details")
                with tag('tr'):
                    line('td', "Total");
                    line('td', str(combined_stats.num_tactics))
                    line('td', stringified_percent(combined_stats.num_correct,
                                                   combined_stats.num_tactics))
                    line('td', stringified_percent(combined_stats.num_topN,
                                                   combined_stats.num_tactics))
                    line('td', stringified_percent(combined_stats.num_partial,
                                                   combined_stats.num_tactics))
                    line('td', stringified_percent(combined_stats.num_topNPartial,
                                                   combined_stats.num_tactics))
                    line('td', "{:10.2f}".format(combined_stats.total_loss /
                                                 combined_stats.num_tactics))

    for filename in extra_files:
        shutil.copy(os.path.dirname(os.path.abspath(__file__)) + "/../reports/" + filename,
                    args.output + "/" + filename)

    with open("{}/report.html".format(args.output), "w") as fout:
        fout.write(doc.getvalue())

def header(tag : Tag, doc : Doc, text : Text, css : List[str],
           javascript : List[str], title : str) -> None:
    with tag('head'):
        for filename in css:
            doc.stag('link', href=filename, rel='stylesheet')
        for filename in javascript:
            with tag('script', type='text/javascript',
                     src=filename):
                pass
        with tag('title'):
            text(title)
def write_html(output_dir : str, filename : str, command_results : List[CommandResult],
               stats : 'ResultStats') -> None:
    def details_header(tag : Any, doc : Doc, text : Text, filename : str) -> None:
        header(tag, doc, text, details_css, details_javascript,
               "Proverbot Detailed Report for {}".format(filename))
    doc, tag, text, line = Doc().ttl()
    with tag('html'):
        details_header(tag, doc, text, filename)
        with tag('div', id='overlay', onclick='event.stopPropagation();'):
            with tag('div', id='predicted'):
                pass
            with tag('div', id='context'):
                pass
            with tag('div', id='stats'):
                pass
            pass
        with tag('body', onclick='deselectTactic()',
                 onload='setSelectedIdx()'), tag('pre'):
            for idx, command_result in enumerate(command_results):
                if len(command_result) == 1:
                    assert isinstance(command_result[0], str)
                    with tag('code', klass='plaincommand'):
                        text("\n" + command_result[0].strip('\n'))
                else:
                    command, hyps, goal, prediction_results = \
                        cast(TacticResult, command_result)
                    predictions, grades, certainties = zip(*prediction_results)
                    with tag('span',
                             ('data-hyps',"\n".join(hyps)),
                             ('data-goal',format_goal(goal)),
                             ('data-num-total', str(stats.num_tactics)),
                             ('data-predictions',
                              to_list_string(cast(List[str], predictions))),
                             ('data-num-predicteds',
                              to_list_string([stats.predicted_tactic_frequency
                                              .get(get_stem(prediction), 0)
                                              for prediction in cast(List[str],
                                                                     predictions)])),
                             ('data-num-corrects',
                              to_list_string([stats.correctly_predicted_frequency
                                              .get(get_stem(prediction), 0)
                                              for prediction in
                                              cast(List[str], predictions)])),
                             ('data-certainties',
                              to_list_string(cast(List[float], certainties))),
                             ('data-num-actual-corrects',
                              stats.correctly_predicted_frequency
                              .get(get_stem(command), 0)),
                             ('data-num-actual-in-file',
                              stats.actual_tactic_frequency
                              .get(get_stem(command), 0)),
                             ('data-actual-tactic',
                              strip_comments(command)),
                             ('data-grades',
                              to_list_string(cast(List[str], grades))),
                             ('data-search-idx', 0),
                             id='command-' + str(idx),
                             onmouseover='hoverTactic({})'.format(idx),
                             onmouseout='unhoverTactic()',
                             onclick='selectTactic({}); event.stopPropagation();'
                             .format(idx)):
                        doc.stag("br")
                        with tag('code', klass=grades[0]):
                            text(command.strip("\n"))
                        for grade in grades[1:]:
                            with tag('span', klass=grade):
                                doc.asis(" &#11044;")
    with open("{}/{}.html".format(output_dir, escape_filename(filename)), "w") as fout:
        fout.write(syntax_highlight(doc.getvalue()))

    pass
def write_csv(output_dir : str, filename : str, args : argparse.Namespace,
              command_results : List[CommandResult], stats : 'ResultStats') -> None:
    with open("{}/{}.csv".format(output_dir, escape_filename(filename)),
              'w', newline='') as csvfile:
        for k, v in vars(args).items():
            csvfile.write("# {}: {}\n".format(k, v))

        rowwriter = csv.writer(csvfile, lineterminator=os.linesep)
        for row in command_results:
            if len(row) == 1:
                rowwriter.writerow([re.sub(r"\n", r"\\n", cast(str, row[0]))])
            else:
                # Type hack
                command, hyps, goal, prediction_results = cast(TacticResult, row)

                rowwriter.writerow([re.sub(r"\n", r"\\n", item) for item in
                                    [command] +
                                    hyps +
                                    [goal] +
                                    [item
                                     for prediction, grade, certainty in prediction_results
                                     for item in [prediction, grade]]])

def get_file_commands(prelude : str, filename : str) -> List[str]:
    local_filename = prelude + "/" + filename
    loaded_commands = try_load_lin(local_filename)
    if loaded_commands is None:
        print("Warning: this version of the reports can't linearize files! "
              "Using original commands.")
        return load_commands_preserve(local_filename)
    else:
        return loaded_commands

def combine_file_results(results : Iterable['ResultStats']) -> 'ResultStats':
    total = ResultStats("global")
    for result in results:
        total.num_tactics += result.num_tactics
        total.num_correct += result.num_correct
        total.num_partial += result.num_partial
        total.num_failed += result.num_failed
        total.num_topN += result.num_topN
        total.num_topNPartial += result.num_topNPartial
        total.total_loss += result.total_loss
        total.actual_tactic_frequency += result.actual_tactic_frequency
        total.predicted_tactic_frequency += result.predicted_tactic_frequency
        total.correctly_predicted_frequency += result.correctly_predicted_frequency

    return total

class ResultStats:
    def __init__(self, filename : str) -> None:
        self.num_tactics = 0
        self.num_correct = 0
        self.num_partial = 0
        self.num_failed = 0
        self.num_topN = 0
        self.num_topNPartial = 0
        self.total_loss = 0.
        self.actual_tactic_frequency : Counter[str] = collections.Counter()
        self.predicted_tactic_frequency : Counter[str]= collections.Counter()
        self.correctly_predicted_frequency : Counter[str] = collections.Counter()
        self.filename = filename
        self.loss = 0.

    def set_loss(self, loss : float) -> None:
        self.loss = loss

    def add_tactic(self, predictions : List[PredictionResult], correct : str) -> None:
        self.num_tactics += 1

        if predictions[0].grade == "goodcommand" or \
           predictions[0].grade == "mostlygoodcommand":
            self.num_correct += 1
            self.num_partial += 1
            self.correctly_predicted_frequency[get_stem(correct)] += 1
        elif predictions[0].grade == "okaycommand":
            self.num_partial += 1
        else:
            self.num_failed += 1

        for prediction, grade, certainty in predictions:
            if grade == "goodcommand" or \
               grade == "mostlygoodcommand":
                self.num_topN += 1
                break
        for prediction, grade, certainty in predictions:
            if grade == "goodcommand" or \
               grade == "mostlygoodcommand":
                self.num_topNPartial += 1
                break
            if grade == "okaycommand":
                self.num_topNPartial += 1
                break

        self.actual_tactic_frequency[get_stem(correct)] += 1
        self.predicted_tactic_frequency[get_stem(predictions[0].prediction)] += 1

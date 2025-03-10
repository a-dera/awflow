r"""
Definition of the Slurm executor.
"""

import os
import shutil
import tempfile

from awflow import plugins
from awflow.dawg import DirectedAcyclicWorkflowGraph as DAWG
from awflow.node import Node
from awflow.utils.executor import executable_name
from awflow.utils.executor import generate_executables
from awflow.utils.executor import generate_metadata
from awflow.utils.executor import generate_postconditions



def execute(workflow: DAWG, dir: str = '.workflows', **kwargs) -> None:
    # Preparing the execution files.
    os.makedirs(dir, exist_ok=True)  # Create the base directory
    directory = os.path.abspath(tempfile.mkdtemp(dir=dir))
    program = workflow.program()
    plugins.apply_defaults(workflow=workflow, **kwargs)
    # Postconditions have to be generated before workflow pruning
    generate_postconditions(workflow=workflow, dir=directory)
    workflow.prune()
    # Check if all postconditions have been statisfied
    if len(workflow.nodes) > 0:
        # Generate the necessary files for the graph execution.
        generate_executables(workflow=workflow, dir=directory)
        generate_metadata(workflow=workflow, dir=directory)
        # Prepare the Slurm submission.
        prepare_submission(workflow=workflow, dir=directory)
        try:
            submit(dir=directory)  # Submit to Slurm
        except Exception as e:
            print(e)
            shutil.rmtree(directory)  # Remove the generated files.
    else:
        # Remove the generated directory
        shutil.rmtree(directory)


def prepare_submission(workflow: DAWG, dir: str) -> None:
    add_default_attributes(workflow=workflow, dir=dir)
    generate_task_files(workflow=workflow, dir=dir)
    generate_submission_script(workflow=workflow, dir=dir)


def add_default_attributes(workflow: DAWG, dir: str) -> None:
    for node in workflow.nodes:
        node['--export='] = 'ALL'  # Exports all environment variables to the job.
        node['--parsable'] = ''   # Enables convenient reading of task ID.
        node['--requeue'] = ''    # Automatically requeue when something fails.
        node['--job-name='] = '"' + node.name + '"'
        # Set the location of the logfile.
        fmt = '"' + dir + '/logs/' + node.name
        if node.tasks > 1:
            fmt += '-%A_%a.log'
        else:
            fmt += '-%j.log'
        node['--output='] = fmt + '"'


def generate_task_files(workflow: DAWG, dir: str) -> None:
    for node in workflow.nodes:
        lines = []
        lines.append('#!/usr/bin/env bash')
        # Set the task attributes
        for key in node.attributes:
            if key[:2] != '--':  # Skip non-sbatch arguments
                continue
            value = node[key]
            line = '#SBATCH ' + key + value
            lines.append(line)
        # Check if the task is an array task.
        if node.tasks > 1:
            lines.append('#SBATCH --array 0-' + str(node.tasks - 1))
            command_suffix = ' $SLURM_ARRAY_TASK_ID'
        else:
            command_suffix = ''
        # Generate the commands of the task
        commands = []
        commands.extend(plugins.generate_before(node=node))
        absolute_path = os.path.abspath(dir + '/' + executable_name(node))
        commands.append('python -u -m awflow.bin.processor ' + absolute_path + command_suffix)
        commands.extend(plugins.generate_after(node=node))
        lines.extend(commands)
        # Write the task file.
        with open(dir + '/' + node_task_filename(node), 'w') as f:
            for line in lines:
                f.write(line + '\n')


def node_task_filename(node):
    return str(id(node)) + ".sbatch"


def generate_submission_script(workflow: DAWG, dir: str) -> None:
    lines = []
    lines.append('#!/usr/bin/env bash')
    lines.append('mkdir -p {}/logs'.format(dir))
    job_identifiers = 'echo "'
    tasks = {}
    for task_index, task in enumerate(workflow.program()):
        tasks[task.identifier] = task_index  # Book-keeping for dependencies.
        # Generate the line in the submission script.
        line = 't' + str(task_index) + '=$(sbatch '
        if len(task.dependencies) > 0:
            flag = '--dependency=afterok'
            for dependency in task.dependencies:
                identifier = tasks[dependency.identifier]
                flag += ':$t' + str(identifier)
            line += flag + ' '
        line += dir + '/' + node_task_filename(task) + ')'
        lines.append(line)
        # Add the generated job identifier.
        job_identifiers += '$t' + str(task_index) + '\n'
    job_identifiers += '" > ' + dir + '/job_identifiers'
    with open(dir + '/pipeline', 'w') as f:
        for line in lines:
            f.write(line + '\n')
        f.write(job_identifiers)


def submit(dir: str) -> None:
    return_code = os.system('bash ' + dir + '/pipeline')
    if return_code != 0:
        raise Exception("Failed to submit pipeline.")
    print("Pipeline submitted. Logfiles are stored at `{}`".format(dir + '/logs'))

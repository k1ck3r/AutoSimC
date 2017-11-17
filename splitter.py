import os
import shutil
import sys
import subprocess
import time
import datetime
import logging
import concurrent.futures

from settings import settings

# change path accordingly to your location
# don´t forget to add double-backslash for subdirs, as shown below
try:
    simc_path = settings.simc_path
except AttributeError:
    # set path after downloading nightly
    pass

subdir1 = settings.subdir1
subdir2 = settings.subdir2
subdir3 = settings.subdir3

single_actor_batch = settings.simc_single_actor_batch

user_targeterror = 0.0


def parse_profiles_from_file(fd, user_class):
    """Parse a simc file, and yield each player entry (between two class=name lines)"""
    current_profile = []
    for line in fd:
        line = line.rstrip()  # Remove trailing \n
        if line.startswith(user_class + "="):
            if len(current_profile):
                yield current_profile
                current_profile = []
        current_profile.append(line)
    # Add tail
    if len(current_profile):
        yield current_profile


def dump_profiles_to_file(filename, profiles):
    logging.debug("Writing {} profiles to file {}.".format(len(profiles), filename))
    with open(filename, "w") as out:
        for line in profiles:
            out.write(line)


# deletes and creates needed folders
# sometimes it generates a permission error; do not know why (am i removing and recreating too fast?)
def purge_subfolder(subfolder, retries=3):
    if not os.path.exists(subfolder):
        try:
            os.makedirs(subfolder)
        except PermissionError:
            if retries < 0:
                print("Error creating folders, pls check your permissions.")
                sys.exit(1)
            print("Error creating folder, retrying in 3 secs")
            time.sleep(3000)
            purge_subfolder(subfolder, retries - 1)
    else:
        shutil.rmtree(subfolder)
        purge_subfolder(subfolder, retries)


def split(inputfile, size, wow_class):
    """
    Split a .simc file into n pieces
    calculations are therefore done much more memory-efficient; simcraft usually crashes the system if too many profiles
    have to be simulated at once
    inputfile: the output of main.py with all permutations in a big file
    size: after size profiles a new file will be created, incrementally numbered
    """
    if size <= 0:
        raise ValueError("Invalid split size {} <= 0.".format(size))

    bestprofiles = []
    outfile_count = 0
    subfolder = os.path.join(os.getcwd(), subdir1)
    purge_subfolder(subfolder)
    with open(inputfile, encoding='utf-8', mode="r") as src:
        for profile in parse_profiles_from_file(src, wow_class):
            profile.append("")  # Add tailing empty line
            bestprofiles.append("\n".join(profile))
            if len(bestprofiles) >= size:
                outfile = os.path.join(subfolder, "sim" + str(outfile_count) + ".sim")
                dump_profiles_to_file(outfile, bestprofiles)
                bestprofiles.clear()
                outfile_count += 1
    # Write tail
    if len(bestprofiles):
        outfile = os.path.join(subfolder, "sim" + str(outfile_count) + ".sim")
        dump_profiles_to_file(outfile, bestprofiles)
        outfile_count += 1


def generateCommand(file, output, sim_type, stage3, multisim, player_profile):
    cmd = []
    cmd.append(os.path.normpath(simc_path))
    cmd.append('ptr=' + str(settings.simc_ptr))
    cmd.append(file)
    cmd.append(output)
    cmd.append(sim_type)
    if multisim:
        cmd.append('threads=' + str(settings.number_of_threads))
    else:
        cmd.append('threads=' + str(settings.simc_threads))
    cmd.append('fight_style=' + str(settings.default_fightstyle))
    cmd.append('input=' + os.path.join(os.getcwd(), settings.additional_input_file))
    cmd.append('process_priority=' + str(settings.simc_priority))
    cmd.append('single_actor_batch=' + str(single_actor_batch))
    if stage3:
        if settings.simc_scale_factors_stage3:
            cmd.append('calculate_scale_factors=1')
            if player_profile.class_role == "strattack":
                cmd.append('scale_only=str,crit,haste,mastery,vers')
            elif player_profile.class_role == "agiattack":
                cmd.append('scale_only=agi,crit,haste,mastery,vers')
            elif player_profile.class_role == "spell":
                cmd.append('scale_only=int,crit,haste,mastery,vers')
    return cmd


def worker(command, counter, maximum, starttime, num_workers):
    print("-----------------------------------------------------------------")
    print(F"Currently processing: {command[2]}")
    print(F"Processing: {counter+1}/{maximum} ({round(100 * float(int(counter) / int(maximum)), 1)}%)")
    try:
        if counter > 0 and counter % num_workers == 0:
            duration = datetime.datetime.now() - starttime
            avg_calctime_hist = duration / counter
            remaining_time = (maximum - counter) * avg_calctime_hist
            finish_time = datetime.datetime.now() + remaining_time
            print(F"Remaining calculation time (est.): {remaining_time}.")
            print(F"Finish time (est.): {finish_time}")
    except Exception:
        logging.debug("Error while calculating progress time.", exc_info=True)

    if settings.multi_sim_disable_console_output and maximum > 1:
        FNULL = open(os.devnull, 'w')  # thx @cwok for working this out
        p = subprocess.Popen(command, stdout=FNULL, stderr=FNULL)
    else:
        p = subprocess.Popen(command)
    r = p.wait()
    if r != 0:
        logging.error("Simulation #{} returned error code {}.".format(counter, r))
    return r


def launch_simc_commands(commands):
    starttime = datetime.datetime.now()

    print("-----------------------------------------------------------------")
    print("Starting multi-process simulation.")
    print("Number of work items: {}.".format(len(commands)))
    print("Number of worker instances: {}.".format(settings.number_of_instances))
    try:
        num_workers = settings.number_of_instances
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=num_workers,
                                                         thread_name_prefix="SimC-Worker")
        counter = 0
        results = []
        for command in commands:
            results.append(executor.submit(worker, command, counter, len(commands), starttime, num_workers))
            counter += 1

        # Check if we got any simulations with error code != 0. futures.as_completed gives us the results as soon as a
        # simulation is finished.
        for future in concurrent.futures.as_completed(results):
            r = int(future.result())
            if r != 0:
                logging.error("Invalid return code from SimC: {}".format(r))
                # Hacky way to shut down all remaining sims, apparently just calling shutdown(wait=False0 on the
                # executor does not have the same effect.
                for f in results:
                    f.cancel()
                executor.shutdown(wait=False)
                return False
        executor.shutdown()
        return True
    except KeyboardInterrupt:
        logging.info("KeyboardInterrupt in simc executor. Stopping.")
        for f in results:
            f.cancel()
        executor.shutdown(wait=False)
        raise
    return False


def start_multi_sim(files_to_sim, player_profile, simtype, command):
    output_time = "{:%Y-%m-%d_%H-%M-%S}".format(datetime.datetime.now())

    # some minor progress-bar-initialization
    amount_of_generated_splits = 0
    for file in files_to_sim:
        if file.endswith(".sim"):
            amount_of_generated_splits += 1

    commands = []
    for file in files_to_sim:
        if file.endswith(".sim"):
            name = file[0:file.find(".")]
            if command <= 1:
                cmd = generateCommand(file,
                                      'output=' + file + '.result',
                                      simtype,
                                      False,
                                      True,
                                      player_profile)
            if command == 2:
                cmd = generateCommand(file,
                                      'html=' + name + "-" + str(output_time) + '.html',
                                      simtype, True, True,
                                      player_profile)
            commands.append(cmd)
    return launch_simc_commands(commands)


# chooses settings and multi- or singlemode smartly
def sim(subdir, simtype, player_profile, command=1):
    subdir = os.path.join(os.getcwd(), subdir)
    files = os.listdir(subdir)
    files = [f for f in files if not f.endswith(".result")]
    files = [os.path.join(subdir, f) for f in files]

    start = datetime.datetime.now()
    result = start_multi_sim(files, player_profile, simtype, command)
    end = datetime.datetime.now()
    logging.info("Simulation took {}.".format(end-start))
    return result


def filter_by_length(dps_results, n):
    """
    filter dps list to only contain n results
    dps_results is a pre-sorted list (dps, name) in descending order
    """
    return dps_results[:n]


def filter_by_target_error(dps_results, target_error):
    """
    remove all profiles not within the errorrange of the best player
    dps_results is a pre-sorted list (dps, name) in descending order
    """
    if len(dps_results) > 2:
        dps_best_player = dps_results[0][0]
        dps_min = dps_best_player * (1.0 - (settings.default_error_rate_multiplier * target_error) / 100.0)
        logging.debug("Filtering out all players below dps_min={}".format(dps_min))
        dps_results = [e for e in dps_results if e[0] >= dps_min]
    return dps_results


# determine best n dps-simulations and grabs their profiles for further simming
# targeterror: the span which removes all profile-dps not fulfilling it (see settings.py)
# source_subdir: directory of .result-files
# target_subdir: directory to store the resulting .sim-file
# origin: path to the originally in autosimc generated output-file containing all valid profiles
def grab_best(filter_by, filter_criterium, source_subdir, target_subdir, origin):
    print("Grabbest:")
    print("Variables: filter by: " + str(filter_by))
    print("Variables: filter_criterium: " + str(filter_criterium))
    print("Variables: target_subdir: " + str(target_subdir))
    print("Variables: origin: " + str(origin))

    user_class = ""

    best = []
    source_subdir = os.path.join(os.getcwd(), source_subdir)
    print("Variables: source_subdir: " + str(source_subdir))
    files = os.listdir(source_subdir)
    files = [f for f in files if f.endswith(".result")]
    files = [os.path.join(source_subdir, f) for f in files]
    logging.debug("Grabbing files: {}".format(files))

    for file in files:
        if os.stat(file).st_size <= 0:
            raise RuntimeError("Error: .result-file in: " + str(source_subdir) + " is empty, exiting")

        with open(file, encoding='utf-8', mode="r") as src:
            for line in src.readlines():
                line = line.lstrip().rstrip()
                if not line:
                    continue
                if line.rstrip().startswith("Raid"):
                    continue
                if line.rstrip().startswith("raid_event"):
                    continue
                if line.rstrip().startswith("HPS"):
                    continue
                if line.rstrip().startswith("DPS"):
                    continue
                # here parsing stops, because its useless profile-junk
                if line.rstrip().startswith("DPS:"):
                    break
                if line.rstrip().endswith("Raid"):
                    continue
                # just get user_class from player_info, very dirty
                if line.rstrip().startswith("Player"):
                    _player, _profile_name, _race, wow_class, *_tail = line.split()
                    user_class = wow_class
                    break
                # dps, percentage, profilename
                dps, _pct, profile_name = line.lstrip().rstrip().split()
                # print("Splitted_lines = a: "+str(a)+" b: "+str(b)+" c: "+str(c))
                # put dps as key and profilename as value into dictionary
                # dps might be equal for 2 profiles, but should very rarely happen
                # could lead to a problem with very minor dps due to variance,
                # but seeing dps going into millions nowadays equal dps should not pose to be a problem at all
                best.append((float(dps), profile_name))

    # sort best dps, descending order
    best = list(reversed(sorted(best, key=lambda entry: entry[0])))
    logging.debug("Result from parsing dps len={}".format(len(best)))
    for dps, name in best:
        logging.debug("{}: {}".format(dps, name))

    if filter_by == "target_error":
        best = filter_by_target_error(best, filter_criterium)
    elif filter_by == "count":
        best = filter_by_length(best, filter_criterium)
    else:
        raise ValueError("Invalid filter")

    logging.debug("Filtered dps results len={}".format(len(best)))
    for dps, name in best:
        logging.debug("{}: {}".format(dps, name))

    sortednames = [name for _dps, name in best]

    bestprofiles = []
    outfile_count = 0
    num_profiles = 0
    # print(str(bestprofiles))

    # Determine chunk length we want to split the profiles
    if target_subdir == settings.subdir2:
        if settings.multi_sim_enabled:
            chunk_length = int(len(sortednames) // settings.number_of_instances)+1
    else:
        chunk_length = settings.splitting_size
    if chunk_length < 1:
        chunk_length = 1
    if chunk_length > settings.splitting_size:
        chunk_length = settings.splitting_size
    logging.debug("Chunk length: {}".format(chunk_length))

    # now parse our "database" and extract the profiles of our top n
    logging.debug("Getting sim input from file {}.".format(origin))
    with open(origin, "r") as source:
        subfolder = os.path.join(os.getcwd(), target_subdir)
        purge_subfolder(subfolder)
        for profile in parse_profiles_from_file(source, user_class):
            _classname, profilename = profile[0].split("=")
            if profilename in sortednames:
                profile.append("")  # Add tailing empty line
                bestprofiles.append("\n".join(profile))
                num_profiles += 1
                logging.debug("Added {} to best list.".format(profilename))
                # If we reached chunk length, dump collected profiles and reset, so we do not store everything in memory
                if len(bestprofiles) >= chunk_length:
                    outfile = os.path.join(os.getcwd(), target_subdir, "best" + str(outfile_count) + ".sim")
                    dump_profiles_to_file(outfile, bestprofiles)
                    bestprofiles.clear()
                    outfile_count += 1

    # Write tail
    if len(bestprofiles):
        outfile = os.path.join(os.getcwd(), target_subdir, "best" + str(outfile_count) + ".sim")
        dump_profiles_to_file(outfile, bestprofiles)
        outfile_count += 1

    logging.info("Got {} best profiles written to {} files..".format(num_profiles, outfile_count))

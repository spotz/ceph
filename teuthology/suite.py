# this file is responsible for submitting tests into the queue
# by generating combinations of facets found in
# https://github.com/ceph/ceph-qa-suite.git

import argparse
import copy
import errno
import itertools
import logging
import os
import re
import subprocess
import sys
import tempfile
from textwrap import dedent, fill
import time
import yaml

from teuthology import misc as teuthology
from teuthology import safepath
from teuthology import lock as lock
from teuthology.config import config

log = logging.getLogger(__name__)

def main():
    parser = argparse.ArgumentParser(description="""
Run a suite of ceph integration tests.

A suite is a set of collections.

A collection is a directory containing facets.

A facet is a directory containing config snippets.

Running a collection means running teuthology for every configuration
combination generated by taking one config snippet from each facet.

Any config files passed on the command line will be used for every
combination, and will override anything in the suite.
""")
    parser.add_argument(
        '-v', '--verbose',
        action='store_true', default=None,
        help='be more verbose',
        )
    parser.add_argument(
        '--dry-run',
        action='store_true', default=None,
        help='do a dry run; do not schedule anything',
        )
    parser.add_argument(
        '--name',
        help='name for this suite',
        required=True,
        )
    parser.add_argument(
        '--base',
        default=None,
        help='base directory for the collection(s)'
        )
    parser.add_argument(
        '--collections',
        metavar='DIR',
        nargs='+',
        required=True,
        help='the collections to run',
        )
    parser.add_argument(
        '--owner',
        help='job owner',
        )
    parser.add_argument(
        '--email',
        help='address to email test failures to',
        )
    parser.add_argument(
        '--timeout',
        help='how many seconds to wait for jobs to finish before emailing results',
        )
    parser.add_argument(
        '-n', '--num',
        default=1,
        type=int,
        help='number of times to run/queue each job'
        )
    parser.add_argument(
        '-w', '--worker',
        default='plana',
        help='which worker to use (type of machine)',
        )
    parser.add_argument(
        'config',
        metavar='CONFFILE',
        nargs='*',
        default=[],
        help='config file to read',
        )

    args = parser.parse_args()

    loglevel = logging.INFO
    if args.verbose:
        loglevel = logging.DEBUG

    logging.basicConfig(
        level=loglevel,
        )

    base_arg = [
        os.path.join(os.path.dirname(sys.argv[0]), 'teuthology-schedule'),
        '--name', args.name,
        '--num', str(args.num),
        '--worker', args.worker,
        ]
    if args.verbose:
        base_arg.append('-v')
    if args.owner:
        base_arg.extend(['--owner', args.owner])

    for collection in args.collections:
        p = os.path.join(args.base, collection)
        if not os.path.isdir(p):
            print >>sys.stderr, 'Collection %s is not a directory' % p
            sys.exit(1)

    collections = [
        (os.path.join(args.base, collection), collection)
        for collection in args.collections
        ]

    for collection, collection_name in sorted(collections):
        log.info('Collection %s in %s' % (collection_name, collection))
        configs = [(combine_path(collection_name, a[0]), a[1]) for a in build_matrix(collection)]

        arch = get_arch(args.config)
        machine_type = get_machine_type(args.config)
        for description, config in configs:
            y = yaml.load(config)

            os_type = y.get('os_type')
            exclude_arch = y.get('exclude_arch')
            exclude_os_type = y.get('exclude_os_type')

            if exclude_arch:
                if exclude_arch == arch:
                    log.info(
                        'Skipping due to excluded_arch: %s facets %s', exclude_arch, description
                         )
                    continue
            if exclude_os_type:
                if exclude_os_type == os_type:
                    log.info(
                        'Skipping due to excluded_os_type: %s facets %s', exclude_os_type, description
                         )
                    continue
            # We should not run multiple tests (changing distros) unless the machine is a VPS
            # Re-imaging baremetal is not yet supported.
            if machine_type != 'vps':
                if os_type and os_type != 'ubuntu':
                    log.info(
                        'Skipping due to non-ubuntu on baremetal facets %s', description
                         )
                    continue

            log.info(
                'Running teuthology-schedule with facets %s', description
                )

            arg = copy.deepcopy(base_arg)
            arg.extend([
                    '--description', description,
                    '--',
                    ])
            arg.extend(args.config)

            temp = tempfile.NamedTemporaryFile()
            try:
                arg.append(temp.name)
                temp.write(config)

                if args.dry_run:
                    log.info('would run: %s' % ' '.join(arg))
                else:
                    subprocess.check_call(
                        args=arg,
                        )
            finally:
                temp.close()

    arg = copy.deepcopy(base_arg)
    arg.append('--last-in-suite')
    if args.email:
        arg.extend(['--email', args.email])
    if args.timeout:
        arg.extend(['--timeout', args.timeout])
    subprocess.check_call(
        args=arg,
        )


def combine_path(left, right):
    """
    os.path.join(a, b) doesn't like it when b is None
    """
    if right:
        return os.path.join(left, right)
    return left

def build_matrix(path):
    """
    return a list of items describe by path
    """
    mode = os.stat(path).st_mode
    if stat.S_ISREG(mode):
        #print 'reg %s' % path
        if path.endswith('.yaml'):
            with file(path, 'r') as f:
                return [(None, f.read())]
    if stat.S_ISDIR(mode):
        files = sorted(os.listdir(path))
        if '+' in files:
            # concatenate items
            #print 'concat %s' % path
            files.remove('+')
            out = []
            for fn in files:
                out.extend(build_matrix(os.path.join(path, fn)))
            return [(
                    '+',
                    ['\n'.join([a[1] for a in out])]
                    )]
        elif '%' in files:
            # convolve items
            #print 'convolve %s' % path
            files.remove('%')
            items = []
            for fn in files:
                raw = build_matrix(os.path.join(path, fn))
                items.append([(combine_path(fn, a[0]), a[1]) for a in raw])
            out = []
            for a in itertools.product(*items):
                name = '{' + ' '.join([i[0] for i in a]) + '}'
                val = '\n'.join([i[1] for i in a])
                out.append((name, val))
            return out
        else:
            # list items
            #print 'list %s' % path
            out = []
            for fn in files:
                raw = build_matrix(os.path.join(path, fn))
                out.extend([(combine_path(fn, a[0]), a[1]) for a in raw])
            return out

def ls():
    parser = argparse.ArgumentParser(description='List teuthology job results')
    parser.add_argument(
        '--archive-dir',
        metavar='DIR',
        help='path under which to archive results',
        required=True,
        )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true', default=False,
        help='show reasons tests failed',
        )
    args = parser.parse_args()

    for j in get_jobs(args.archive_dir):
        job_dir = os.path.join(args.archive_dir, j)
        summary = {}
        try:
            with file(os.path.join(job_dir, 'summary.yaml')) as f:
                g = yaml.safe_load_all(f)
                for new in g:
                    summary.update(new)
        except IOError, e:
            if e.errno == errno.ENOENT:
                print '%s      ' % j,

                # pid
                try:
                    pidfile = os.path.join(job_dir, 'pid')
                    found = False
                    if os.path.isfile(pidfile):
                        pid = open(pidfile, 'r').read()
                        if os.path.isdir("/proc/%s" % pid):
                            cmdline = open('/proc/%s/cmdline' % pid, 'r').read()
                            if cmdline.find(args.archive_dir) >= 0:
                                print '(pid %s)' % pid,
                                found = True
                    if not found:
                        print '(no process or summary.yaml)',
                    # tail
                    tail = os.popen(
                        'tail -1 %s/%s/teuthology.log' % (args.archive_dir, j)
                        ).read().rstrip()
                    print tail,
                except IOError, e:
                    continue
                print ''
                continue
            else:
                raise

        print "{job} {success} {owner} {desc} {duration}s".format(
            job=j,
            owner=summary.get('owner', '-'),
            desc=summary.get('description', '-'),
            success='pass' if summary.get('success', False) else 'FAIL',
            duration=int(summary.get('duration', 0)),
            )
        if args.verbose and 'failure_reason' in summary:
            print '    {reason}'.format(reason=summary['failure_reason'])

def generate_coverage(args):
    log.info('starting coverage generation')
    subprocess.Popen(
        args=[
            os.path.join(os.path.dirname(sys.argv[0]), 'teuthology-coverage'),
            '-v',
            '-o',
            os.path.join(args.teuthology_config['coverage_output_dir'], args.name),
            '--html-output',
            os.path.join(args.teuthology_config['coverage_html_dir'], args.name),
            '--cov-tools-dir',
            args.teuthology_config['coverage_tools_dir'],
            args.archive_dir,
            ],
        )

def email_results(subject, from_, to, body):
    log.info('Sending results to {to}: {body}'.format(to=to, body=body))
    import smtplib
    from email.mime.text import MIMEText
    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = from_
    msg['To'] = to
    log.debug('sending email %s', msg.as_string())
    smtp = smtplib.SMTP('localhost')
    smtp.sendmail(msg['From'], [msg['To']], msg.as_string())
    smtp.quit()

def results():
    parser = argparse.ArgumentParser(description='Email teuthology suite results')
    parser.add_argument(
        '--email',
        help='address to email test failures to',
        )
    parser.add_argument(
        '--timeout',
        help='how many seconds to wait for all tests to finish (default no wait)',
        type=int,
        default=0,
        )
    parser.add_argument(
        '--archive-dir',
        metavar='DIR',
        help='path under which results for the suite are stored',
        required=True,
        )
    parser.add_argument(
        '--name',
        help='name of the suite',
        required=True,
        )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true', default=False,
        help='be more verbose',
        )
    args = parser.parse_args()

    loglevel = logging.INFO
    if args.verbose:
        loglevel = logging.DEBUG

    logging.basicConfig(
        level=loglevel,
        )

    teuthology.read_config(args)

    handler = logging.FileHandler(
        filename=os.path.join(args.archive_dir, 'results.log'),
        )
    formatter = logging.Formatter(
        fmt='%(asctime)s.%(msecs)03d %(levelname)s:%(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S',
        )
    handler.setFormatter(formatter)
    logging.getLogger().addHandler(handler)

    try:
        _results(args)
    except:
        log.exception('error generating results')
        raise


def _results(args):
    running_tests = [
        f for f in sorted(os.listdir(args.archive_dir))
        if not f.startswith('.')
        and os.path.isdir(os.path.join(args.archive_dir, f))
        and not os.path.exists(os.path.join(args.archive_dir, f, 'summary.yaml'))
    ]
    starttime = time.time()
    log.info('Waiting up to %d seconds for tests to finish...', args.timeout)
    while running_tests and args.timeout > 0:
        if os.path.exists(os.path.join(
                args.archive_dir,
                running_tests[-1], 'summary.yaml')):
            running_tests.pop()
        else:
            if time.time() - starttime > args.timeout:
                log.warn('test(s) did not finish before timeout of %d seconds',
                         args.timeout)
                break
            time.sleep(10)
    log.info('Tests finished! gathering results...')

    (subject, body) = build_email_body(args.name, args.archive_dir,
                                       args.timeout)

    try:
        if args.email:
            email_results(
                subject=subject,
                from_=args.teuthology_config['results_sending_email'],
                to=args.email,
                body=body,
            )
    finally:
        generate_coverage(args)


def get_http_log_path(archive_dir, job_id):
    http_base = config.archive_server
    if not http_base:
        return None
    archive_subdir = os.path.split(archive_dir)[-1]
    return os.path.join(http_base, archive_subdir, str(job_id))


def get_jobs(archive_dir):
    dir_contents = os.listdir(archive_dir)

    def is_job_dir(parent, subdir):
        if os.path.isdir(os.path.join(parent, subdir)) and re.match('\d+$', subdir):
            return True
        return False

    jobs = [job for job in dir_contents if is_job_dir(archive_dir, job)]
    return sorted(jobs)


email_templates = {
    'body_templ': dedent("""\
        Test Run
        NOTE: Apologies for links inside the Inktank firewall; we are working to make them public.
        =================================================================
        logs:   {log_root}
        failed: {fail_count}
        hung:   {hung_count}
        passed: {pass_count}

        {fail_sect}{hung_sect}{pass_sect}
        """),
    'sect_templ': dedent("""\
        {title}
        =================================================================
        {jobs}
        """),
    'fail_templ': dedent("""\
        [{job_id}]  {desc}
        -----------------------------------------------------------------
        time:   {time}s{log_line}{sentry_line}

        {reason}

        """),
    'fail_log_templ': "\nlog:    {log}",
    'fail_sentry_templ': "\nsentry: {sentries}",
    'hung_templ': dedent("""\
        [{job_id}]
        """),
    'pass_templ': dedent("""\
        [{job_id}] {desc}
        time:    {time}s

        """),
}



def build_email_body(name, archive_dir, timeout):
    failed = {}
    hung = {}
    passed = {}

    for job in get_jobs(archive_dir):
        job_dir = os.path.join(archive_dir, job)
        summary_file = os.path.join(job_dir, 'summary.yaml')

        # Unfinished jobs will have no summary.yaml
        if not os.path.exists(summary_file):
            hung[job] = email_templates['hung_templ'].format(job_id=job)
            continue

        with file(summary_file) as f:
            summary = yaml.safe_load(f)

        if summary['success']:
            passed[job] = email_templates['pass_templ'].format(
                job_id=job,
                desc=summary.get('description'),
                time=int(summary.get('duration')),
            )
        else:
            log = get_http_log_path(archive_dir, job)
            if log:
                log_line = email_templates['fail_log_templ'].format(log=log)
            else:
                log_line = ''
            sentry_events = summary.get('sentry_events')
            if sentry_events:
                sentry_line = email_templates['fail_sentry_templ'].format(
                    sentries='\n        '.join(sentry_events))
            else:
                sentry_line = ''

            # 'fill' is from the textwrap module and it collapses a given
            # string into multiple lines of a maximum width as specified. We
            # want 75 characters here so that when we indent by 4 on the next
            # line, we have 79-character exception paragraphs.
            reason = fill(summary.get('failure_reason'), 75)
            reason = '\n'.join(('    ') + line for line in reason.splitlines())

            failed[job] = email_templates['fail_templ'].format(
                job_id=job,
                desc=summary.get('description'),
                time=int(summary.get('duration')),
                reason=reason,
                log_line=log_line,
                sentry_line=sentry_line,
            )

    maybe_comma = lambda s: ', ' if s else ' '

    subject = ''
    fail_sect = ''
    hung_sect = ''
    pass_sect = ''
    if failed:
        subject += '{num_failed} failed{sep}'.format(
            num_failed=len(failed),
            sep=maybe_comma(hung or passed)
        )
        fail_sect = email_templates['sect_templ'].format(
            title='Failed',
            jobs=''.join(failed.values())
        )
    if hung:
        subject += '{num_hung} hung{sep}'.format(
            num_hung=len(hung),
            sep=maybe_comma(passed),
        )
        hung_sect = email_templates['sect_templ'].format(
            title='Hung',
            jobs=''.join(hung.values()),
        )
    if passed:
        subject += '%s passed ' % len(passed)
        pass_sect = email_templates['sect_templ'].format(
            title='Passed',
            jobs=''.join(passed.values()),
        )

    body = email_templates['body_templ'].format(
        log_root=get_http_log_path(archive_dir, ''),
        fail_count=len(failed),
        hung_count=len(hung),
        pass_count=len(passed),
        fail_sect=fail_sect,
        hung_sect=hung_sect,
        pass_sect=pass_sect,
    )

    subject += 'in {suite}'.format(suite=name)
    return (subject.strip(), body.strip())


def get_arch(config):
    for yamlfile in config:
        y = yaml.safe_load(file(yamlfile))
        machine_type = y.get('machine_type')
        if machine_type:
            fakectx = []
            locks = lock.list_locks(fakectx)
            for machine in locks:
                if machine['type'] == machine_type:
                    arch = machine['arch']
                    return arch
    return None


def get_os_type(configs):
    for config in configs:
        yamlfile = config[2]
        y = yaml.safe_load(file(yamlfile))
        if not y:
            y = {}
        os_type = y.get('os_type')
        if os_type:
            return os_type
    return None


def get_exclude_arch(configs):
    for config in configs:
        yamlfile = config[2]
        y = yaml.safe_load(file(yamlfile))
        if not y:
            y = {}
        exclude_arch = y.get('exclude_arch')
        if exclude_arch:
            return exclude_arch
    return None


def get_exclude_os_type(configs):
    for config in configs:
        yamlfile = config[2]
        y = yaml.safe_load(file(yamlfile))
        if not y:
            y = {}
        exclude_os_type = y.get('exclude_os_type')
        if exclude_os_type:
            return exclude_os_type
    return None


def get_machine_type(config):
    for yamlfile in config:
        y = yaml.safe_load(file(yamlfile))
        if not y:
            y = {}
        machine_type = y.get('machine_type')
        if machine_type:
            return machine_type
    return None


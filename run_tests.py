#!/usr/bin/env python
from __future__ import print_function
import os, sys, subprocess

class Object(object): pass

UNIT = '-u'
INTEGRATION = '-i'
actions = (UNIT, INTEGRATION)

action = sys.argv[1]
assert action in actions, "Expected one of %s" % (", ".join(actions),)
action_name = 'unit' if action == UNIT else 'integration'
args = sys.argv[2:]

cwd = os.getcwd()
kind = os.path.basename(cwd)
kinds = ('python', 'ocaml')
if kind not in kinds:
	kind = None

root = os.path.abspath(os.path.dirname(__file__))
test_dir = os.path.join(root, 'test')

def add_to_env(name, val):
	vals = os.environ.get(name, '').split(os.pathsep)
	vals.insert(0, val)
	os.environ[name] = os.pathsep.join(vals)

add_to_env('PYTHONPATH', os.path.join(os.path.dirname(__file__), 'python'))

try:
	def run_nose(args):
		if os.environ.get('CI', 'false') == 'true':
			args = args + ['-v']
		args = ['--with-doctest', '-w', test_dir] + args

		nose_cmd = 'NOSE_CMD'
		nose_exe = os.environ.get(nose_cmd, None)
		if nose_exe is None:
			with open(os.devnull, 'w') as null:
				if subprocess.Popen(
					['which', '0install'], stdout=null, stderr=subprocess.STDOUT
				).wait() == 0:
					print("Note: running with 0install.", file=sys.stderr)
					subprocess.check_call(['make', '-C', root, 'gup-test-local.xml'])
					subprocess.check_call([
						'0install', 'run', '--command=' + os.environ.get('TEST_COMMAND', 'test'),
						os.path.join(root, 'gup-test-local.xml')] + args)
				else:
					nose_exe = 'nosetests'

		if nose_exe is not None:
			subprocess.check_call(nose_exe.split() + args)

	subprocess.check_call(['make', '%s-test-pre' % action_name])

	if action == INTEGRATION:
		# run without adding to PATH
		if kind is None:
			exe = os.pathsep.join([os.path.join(cwd, kind, 'bin', 'gup') for kind in kinds])
		else:
			exe = os.path.join(cwd, 'bin', 'gup')
		os.environ['GUP_EXE'] = exe
		run_nose(args)
	else:
		assert action == UNIT
		add_to_env('PATH', os.path.join(root, 'test/bin'))
		if kind == 'ocaml':
			subprocess.check_call(['./test.byte', '-runner', 'sequential'] + args)
		else:
			run_nose(args)

except subprocess.CalledProcessError: sys.exit(1)

#!/usr/bin/env python3
import string
import subprocess
import os
import sys
import pipes
import argparse
import json

BUILD = r'''#!/bin/sh
set -e
cd "$(dirname "$0")"

if [ -e nimenv.local ]; then
  echo 'nimenv.local exists. You may use `nimenv build` instead of this script.'
  #exit 1
fi

mkdir -p .nimenv/nim
mkdir -p .nimenv/deps

NIMHASH=@@nimhash
if ! [ -e .nimenv/nimhash -a \( "$(cat .nimenv/nimhash)" = "$NIMHASH" \) ]; then
  echo "Downloading Nim @@nimurl (sha256: $NIMHASH)"
  wget @@nimurl -O .nimenv/nim.tar.xz
  if ! [ "$(sha256sum < .nimenv/nim.tar.xz)" = "$NIMHASH  -" ]; then
    echo "verification failed"
    exit 1
  fi
  echo "Unpacking Nim..."
  rm -r .nimenv/nim
  mkdir -p .nimenv/nim
  cd .nimenv/nim
  tar xJf ../nim.tar.xz
  mv nim-*/* .
  echo "Building Nim..."
  make -j$(getconf _NPROCESSORS_ONLN)
  cd ../..
  echo $NIMHASH > .nimenv/nimhash
fi

get_dep() {
  set -e
  cd .nimenv/deps
  name="$1"
  url="$2"
  hash="$3"
  srcpath="$4"
  new=0
  if ! [ -e "$name" ]; then
    git clone --recursive "$url" "$name"
    new=1
  fi
  if ! [ "$(cd "$name" && git rev-parse HEAD)" = "$hash" -a $new -eq 0 ]; then
     cd "$name"
     git fetch --all
     git checkout -q "$hash"
     git submodule update --init
     cd ..
  fi
  cd ../..
  echo "path: \".nimenv/deps/$name$srcpath\"" >> nim.cfg
}

echo "path: \".\"" > nim.cfg

@@deps

echo @@nimcfg >> nim.cfg

mkdir -p bin
ln -sf ../.nimenv/nim/bin/nim bin/nim

@@build
'''

def split_sections(conf):
    current = []
    result = []
    for line in conf.splitlines():
        if line.startswith('[') and line.endswith(']'):
            current = []
            result.append((line.strip('[]'), current))
        else:
            current.append(line)

    return dict( (k, '\n'.join(v)) for k, v in result)

def parse_kv(conf):
    result = {}
    for line in conf.splitlines():
        line = line.split('#')[0].strip()
        if not line:
            continue

        k, v = line.split(':', 1)
        result[k.strip()] = v.strip()

    return result

def get_rev(cwd):
    null = open('/dev/null', 'w')
    call = lambda cmd: subprocess.call(cmd, shell=True, cwd=cwd, stdout=null, stderr=null, stdin=null)
    if call('git diff --exit-code') or call('git diff --cached --exit-code') or call('git ls-files --other --exclude-standard --directory'):
        print('There are uncommited files in %r!' % cwd, file=sys.stderr)

    commit_id = subprocess.check_output('git rev-parse HEAD', shell=True, cwd=cwd)
    return commit_id.decode().strip()

class MyTemplate(string.Template):
    delimiter = '@@'

def make_dist():
    if not os.path.exists('nimenv.local'):
        sys.exit('nimenv.local doesn\'t exist - use `nimenv localsetup`')
    local = split_sections(open('nimenv.local').read())
    cfg = split_sections(open('nimenv.cfg').read())
    repos = parse_kv(local['repos'])
    deps_raw = parse_kv(cfg['deps'])
    builds = parse_kv(cfg['build'])

    deps = {}
    suffix = {}
    for k, v in deps_raw.items():
        if k == 'nim': continue
        s = v.split(None, 1)
        if len(s) > 1:
            suffix[k] = '/' + s[1]
        else:
            suffix[k] = ''
        deps[k] = s[0]

    nim_cfg = ['path: "."']
    for k, v in sorted(repos.items()):
        nim_cfg.append('path: "%s"' % (v + suffix[k]))
    nim_cfg.append('')
    nim_cfg.append(cfg['nim'] + '\n')

    with open('nim.cfg', 'w') as f:
        f.write('\n'.join(nim_cfg))

    build_script = []
    nim_url, nim_hash = deps_raw['nim'].split(None, 1)

    deps_script = []
    for name, url in sorted(deps.items()):
        rev = get_rev(repos[name])
        deps_script.append('get_dep %s %s %s %s' % (pipes.quote(name), pipes.quote(url), pipes.quote(rev), pipes.quote(suffix[name])))

    nim_script = [
        'echo "building {0}"; bin/nim c -d:release --out:"$PWD/bin/{0}" {1}'.format(pipes.quote(k), pipes.quote(v))
        for k, v in sorted(builds.items())
    ]

    env = {'nimurl': nim_url, 'nimhash': nim_hash, 'nimcfg': pipes.quote(cfg['nim']), 'deps': '\n'.join(deps_script), 'build': '\n'.join(nim_script)}
    build_script = MyTemplate(BUILD).substitute(env)

    if not os.path.exists('deps.nix'):
        with os.fdopen(os.open('build.sh', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o777), 'w') as f:
            f.write(build_script)

    if os.path.exists('deps.nix'):
        try:
            prev = json.load(open('.deps.json'))
        except Exception:
            prev = {}
        new = {}
        nix = ['{fetchgit, ...}:', '{']

        for name, url in deps.items():
            rev = get_rev(repos[name])
            #pkg_url = '%s/archive/%s.tar.gz' % (url, rev)

            if prev.get(name) and prev[name]['rev'] == rev:
                sha256 = prev[name]['sha256']
            else:
                r = subprocess.check_output(['nix-prefetch-git', '--fetch-submodules', url, rev]).strip().decode('utf8')
                sha256 = json.loads(r)['sha256']

            new[name] = {
                'rev': rev,
                'sha256': sha256
            }

            nix += [
                '  %s = fetchgit {' % name,
                '    name = "%s";' % name,
                '    url = "%s";' % url,
                '    rev = "%s";' % rev,
                '    fetchSubmodules = true;',
                '    sha256 = "%s";' % sha256,
                '  };',
            ]

        nix += ['}', '']
        with open('.deps.json', 'w') as f:
            f.write(json.dumps(new, indent=4, sort_keys=True) + '\n')

        with open('deps.nix', 'w') as f:
            f.write('\n'.join(nix))

def local_setup(base_dir):
    if os.path.exists('nimenv.local'):
        local = split_sections(open('nimenv.local').read())
        repos = parse_kv(local.get('repos', ''))
    else:
        repos = {}

    cfg = split_sections(open('nimenv.cfg').read())
    deps_raw = parse_kv(cfg['deps'])

    for k, v in sorted(deps_raw.items()):
        if k == 'nim':
            continue
        url = v.split()[0]
        if k not in repos:
            path = os.path.join(base_dir, k)
            if not os.path.exists(path):
                subprocess.check_call(['git', 'clone', '--recursive', url, path])
            repos[k] = path

    with open('nimenv.local', 'w') as f:
        f.write('[repos]\n')
        for k, v in sorted(repos.items()):
            f.write('%s: %s\n' % (k, v))

    print('nimenv.local created!')

def main():
    if len(sys.argv) == 1:
        sys.argv.append('dist')

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest='command')
    subparser = subparsers.add_parser('dist', help='creates build.sh')
    subparser = subparsers.add_parser('localsetup', help='clones dependencies and creates nimenv.local')
    subparser.add_argument('basedir')

    ns = parser.parse_args()

    if ns.command == 'dist':
        make_dist()
    elif ns.command == 'localsetup':
        local_setup(ns.basedir)
    else:
        parser.print_usage()

if __name__ == '__main__':
    main()

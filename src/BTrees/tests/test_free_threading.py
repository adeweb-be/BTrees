"""Stress test: many threads, one tree for each thread.

Each thread changes its own BTree and TreeSet, scans ranges, stores and
ghostifies the nodes through its own data manager, and resolves conflicts.
On a free-threaded build without the GIL, the threads run in parallel.

A comparison of object keys runs Python code. In some steps, that code
invalidates the buckets of the tree while a search reads one of them.
The search must then raise RuntimeError or give the correct result.

Set BTREES_STRESS_ROUNDS to run more rounds.
"""
import importlib
import os
import random
import sys
import threading
import unittest

from persistent import PickleCache

from BTrees.Interfaces import BTreesConflictError


THREADS = 16
ROUNDS = int(os.environ.get('BTREES_STRESS_ROUNDS', '12'))
KEYSPACE = 2000
FAMILIES = ('OO', 'OI', 'OL', 'IO', 'II', 'IF', 'LO', 'LL', 'QQ', 'UO')

_local = threading.local()


class Key:
    """A key whose comparison runs Python code."""

    __slots__ = ('v',)

    def __init__(self, v):
        self.v = v

    def __lt__(self, other):
        countdown = getattr(_local, 'countdown', 0)
        if countdown:
            # Run the hook in the n-th comparison.
            _local.countdown = countdown - 1
            if countdown == 1:
                _local.hook()
        return self.v < other.v

    def __eq__(self, other):
        return self.v == other.v

    def __hash__(self):
        return hash(self.v)

    def __repr__(self):
        return 'Key(%d)' % self.v


def _raw(key):
    return key.v if isinstance(key, Key) else key


def _nodes(tree):
    """Return the tree, its inner nodes and its buckets."""
    result = [tree]
    state = tree.__getstate__()
    if state is None or len(state) == 1:
        # Empty, or one bucket inside the state of the tree.
        return result
    for child in state[0][::2]:
        if type(child) is type(tree):
            result.extend(_nodes(child))
        else:
            result.append(child)
    return result


class Jar:
    """A data manager for one thread.

    It keeps the last stored state of each object, as a storage does.
    """

    def __init__(self):
        self.cache = PickleCache(self, 0)
        self.states = {}
        self.registered = {}
        self._oid = 0

    def setstate(self, obj):
        obj.__setstate__(self.states[obj._p_oid])

    def register(self, obj):
        self.registered[obj._p_oid] = obj

    def readCurrent(self, obj):
        pass

    def commit(self, *trees):
        for tree in trees:
            for node in _nodes(tree):
                if node._p_oid is None:
                    self._oid += 1
                    node._p_oid = self._oid.to_bytes(8, 'big')
                    node._p_jar = self
                    self.cache[node._p_oid] = node
                self.states[node._p_oid] = node.__getstate__()
                node._p_changed = False
        self.registered.clear()

    def abort(self):
        for obj in list(self.registered.values()):
            obj._p_invalidate()
        self.registered.clear()

    def leaves(self, tree):
        return [node for node in _nodes(tree)
                if type(node) is not type(tree)]


class Worker:

    def __init__(self, index, prefix):
        self.rng = random.Random(index)
        self.module = importlib.import_module('BTrees.%sBTree' % prefix)
        self.prefix = prefix
        self.is_c = 'Py' not in type(self.module.BTree()).__name__
        self.errors = []
        self.reentrant = 0

    def key(self, raw):
        return Key(raw) if self.prefix[0] == 'O' else raw

    def value(self):
        kind = self.prefix[1]
        if kind == 'O':
            return 'v%d' % self.rng.randrange(1000)
        if kind == 'F':
            return self.rng.randrange(1000) / 8.0
        return self.rng.randrange(1 << 20)

    def check(self, tree, ref, treeset, refset):
        keys = [_raw(k) for k in tree.keys()]
        if keys != sorted(ref):
            raise AssertionError('keys differ')
        if [ref[_raw(k)] for k in tree.keys()] != list(tree.values()):
            raise AssertionError('values differ')
        if [_raw(k) for k in treeset] != sorted(refset):
            raise AssertionError('set keys differ')
        if len(tree) != len(ref) or len(treeset) != len(refset):
            raise AssertionError('lengths differ')
        tree._check()
        treeset._check()

    def scan(self, tree, ref):
        lo = self.rng.randrange(KEYSPACE)
        hi = lo + self.rng.randrange(KEYSPACE // 4)
        want = [k for k in sorted(ref) if lo <= k <= hi]
        got = [_raw(k) for k in tree.keys(self.key(lo), self.key(hi))]
        if got != want:
            raise AssertionError('range scan differs')
        want = [k for k in sorted(ref) if lo < k < hi]
        got = [_raw(k) for k, _ in tree.items(
            self.key(lo), self.key(hi), excludemin=True, excludemax=True)]
        if got != want:
            raise AssertionError('exclusive range scan differs')
        if want:
            if _raw(tree.minKey(self.key(lo + 1))) != want[0]:
                raise AssertionError('minKey differs')

    def first_use(self, tree):
        # Use each type of the module once, as all threads start.
        tree[self.key(1)] = self.value()
        it = iter(tree)
        it.__next__
        next(it)
        items = tree.items()
        len(items)
        items[0]
        list(tree.keys(self.key(0), self.key(5)))
        repr(self.module.Set([self.key(1)]))
        repr(self.module.TreeSet([self.key(1)]))
        del tree[self.key(1)]

    def change(self, tree, ref, treeset, refset):
        for _ in range(self.rng.randrange(50, 400)):
            raw = self.rng.randrange(KEYSPACE)
            r = self.rng.random()
            if r < 0.5:
                value = self.value()
                tree[self.key(raw)] = value
                ref[raw] = value
                treeset.add(self.key(raw))
                refset.add(raw)
            elif r < 0.85:
                tree.pop(self.key(raw), None)
                ref.pop(raw, None)
                if raw in refset:
                    treeset.remove(self.key(raw))
                    refset.discard(raw)
            elif r < 0.95:
                self.scan(tree, ref)
            else:
                if tree.get(self.key(raw)) != ref.get(raw):
                    raise AssertionError('get differs')

    def ghostify(self, jar, tree, treeset):
        nodes = _nodes(tree) + _nodes(treeset)
        how = self.rng.randrange(3)
        if how == 0:
            jar.cache.minimize()
        else:
            for node in self.rng.sample(nodes, max(1, len(nodes) // 2)):
                if how == 1:
                    node._p_invalidate()
                else:
                    node._p_deactivate()

    def reenter(self, jar, tree, ref):
        """Invalidate the buckets from inside a comparison."""
        leaves = jar.leaves(tree)

        def hook():
            for bucket in leaves:
                bucket._p_invalidate()

        raw = self.rng.choice(sorted(ref)) if ref else 0
        op = self.rng.randrange(3)
        _local.hook = hook
        _local.countdown = self.rng.randrange(1, 10)
        try:
            if op == 0:
                got = tree.get(self.key(raw))
                if got != ref.get(raw):
                    raise AssertionError('get differs after invalidation')
            elif op == 1:
                got = self.key(raw) in tree
                if got != (raw in ref):
                    raise AssertionError('in differs after invalidation')
            else:
                self.scan(tree, ref)
        except RuntimeError:
            self.reentrant += 1
        finally:
            _local.countdown = 0
            _local.hook = None

    def resolve(self):
        m = self.module
        base_keys = self.rng.sample(range(KEYSPACE), 20)
        base = {k: self.value() for k in base_keys}
        # A merge that deletes the first key is a conflict.
        first = min(base_keys)
        touched = self.rng.sample(sorted(base_keys)[1:], 8)
        committed, new = dict(base), dict(base)
        for k in touched[:2]:
            del committed[k]
        for k in touched[2:4]:
            committed[k] = self.value()
        for k in touched[4:6]:
            del new[k]
        for k in touched[6:]:
            new[k] = self.value()
        extra = self.rng.sample(
            [k for k in range(first, KEYSPACE) if k not in base], 4)
        committed[extra[0]] = self.value()
        committed[extra[1]] = self.value()
        new[extra[2]] = self.value()
        new[extra[3]] = self.value()
        expected = dict(committed)
        for k in touched[4:6]:
            del expected[k]
        for k in touched[6:]:
            expected[k] = new[k]
        expected[extra[2]] = new[extra[2]]
        expected[extra[3]] = new[extra[3]]

        def mapping(cls, d):
            return cls({self.key(k): v for k, v in d.items()})

        def keyset(cls, d):
            return cls([self.key(k) for k in d])

        for cls, build in ((m.Bucket, mapping), (m.BTree, mapping),
                           (m.Set, keyset), (m.TreeSet, keyset)):
            states = [build(cls, d).__getstate__()
                      for d in (base, committed, new)]
            got = cls()._p_resolveConflict(*states)
            if got != build(cls, expected).__getstate__():
                raise AssertionError(
                    '%s resolved state differs' % cls.__name__)
        # Both change the same value: a conflict.
        k = self.rng.choice([k for k in base if k in expected])
        # value() never gives these values.
        committed[k] = 'c' if self.prefix[1] == 'O' else (1 << 21) + 1
        new[k] = 'n' if self.prefix[1] == 'O' else (1 << 21) + 2
        states = [mapping(m.Bucket, d).__getstate__()
                  for d in (base, committed, new)]
        try:
            m.Bucket()._p_resolveConflict(*states)
        except BTreesConflictError:
            pass
        else:
            raise AssertionError('no conflict')

    def run(self, barrier):
        try:
            tree = self.module.BTree()
            barrier.wait()
            self.first_use(tree)
            self.loop(tree)
        except BaseException as e:  # pragma: no cover
            self.errors.append(e)

    def loop(self, tree):
        jar = Jar()
        treeset = self.module.TreeSet()
        ref, refset = {}, set()
        jar.commit(tree, treeset)
        stored, storedset = dict(ref), set(refset)
        for _ in range(ROUNDS):
            self.change(tree, ref, treeset, refset)
            self.check(tree, ref, treeset, refset)
            if self.rng.random() < 0.25:
                jar.abort()
                ref, refset = dict(stored), set(storedset)
            else:
                jar.commit(tree, treeset)
                stored, storedset = dict(ref), set(refset)
            self.check(tree, ref, treeset, refset)
            self.ghostify(jar, tree, treeset)
            self.check(tree, ref, treeset, refset)
            if self.is_c and self.prefix[0] == 'O':
                self.reenter(jar, tree, ref)
                self.check(tree, ref, treeset, refset)
            self.resolve()


class TestFreeThreading(unittest.TestCase):

    def test_one_tree_per_thread(self):
        switch = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            workers = [Worker(i, FAMILIES[i % len(FAMILIES)])
                       for i in range(THREADS)]
            barrier = threading.Barrier(THREADS)
            threads = [threading.Thread(target=w.run, args=(barrier,))
                       for w in workers]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            sys.setswitchinterval(switch)
        errors = [(w.prefix, e) for w in workers for e in w.errors]
        self.assertEqual(errors, [])

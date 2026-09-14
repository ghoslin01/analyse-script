"""Small, lazy structural indexes for files reached by a trace, never a database."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path

from .trace_cache import cache_key, file_fingerprint, load_cache, save_cache
from .xmlbytes import detect_xml_encoding, iter_xml_visible_matches, find_tag_span, read_tag_attributes


def _fingerprint_from_stat(stat) -> dict[str, int]:
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "ctime_ns": stat.st_ctime_ns,
            "inode": stat.st_ino, "device": stat.st_dev}


@dataclass
class TraceNode:
    key: str
    tag: str
    offset: int
    line: int
    meta: dict[str, str]
    parent: TraceNode | None = None
    children: list[TraceNode] = field(default_factory=list)
    attrs: dict[str, str] | None = None


class TraceSource:
    def __init__(self, root: Path, path: Path, max_nodes: int = 100_000, *, extra_metadata=(), cache_dir=None):
        self.path = path.resolve()
        self.relative = self.path.relative_to(root.resolve()).as_posix()
        self.max_nodes = max_nodes
        self.extra_metadata = tuple(sorted(set(extra_metadata)))
        self.cache_dir = Path(cache_dir).resolve() if cache_dir is not None else None
        self.cache_hit = False
        self._cached_issues: dict[str, list[dict]] = {}
        self._replayed_issues: set[str] = set()
        self._dirty = False
        self.before = path.stat()
        self.encoding = detect_xml_encoding(path)
        with path.open('rb') as handle:
            self.sha256 = hashlib.file_digest(handle, 'sha256').hexdigest()
        self._cache_key = cache_key(relative=self.relative, sha256=self.sha256,
                                    extra_metadata=self.extra_metadata, max_nodes=max_nodes)
        if self.cache_dir is not None:
            cached = load_cache(self.cache_dir, self._cache_key, file_fingerprint(self.path))
            if cached is not None and self._restore(cached):
                self.cache_hit = True
                self.check_unchanged()
                return
        self.nodes: list[TraceNode] = []
        self.eids: dict[str, list[TraceNode]] = {}
        self.issues: list[dict] = []
        tags = ('Project', 'Product', 'Phase', 'Rule', 'DataSource', 'Question', 'Button', 'ItemGroup')
        patterns = tuple(p for t in tags for p in ('<' + t, '</' + t))
        stack: list[TraceNode] = []
        line, cursor = 1, 0
        with path.open('rb') as reader, path.open('rb') as lines:
            for hit in iter_xml_visible_matches(path, patterns, encoding=self.encoding):
                span = find_tag_span(path, hit.offset, encoding=self.encoding, handle=reader, containing=False)
                if span is None or span.name not in tags:
                    continue
                while cursor < hit.offset:
                    chunk = lines.read(min(65536, hit.offset - cursor))
                    if not chunk:
                        raise ValueError('File changed while locating lines')
                    line += chunk.count(self.encoding.encode('\n'))
                    cursor += len(chunk)
                if span.is_end:
                    if not stack or stack[-1].tag != span.name:
                        raise ValueError(f'Unbalanced {span.name} at byte {hit.offset}')
                    stack.pop()
                    continue
                if len(self.nodes) >= max_nodes:
                    raise ValueError(f'Structural limit exceeded ({max_nodes} nodes)')
                selected = read_tag_attributes(path, span, lambda k: k in {
                    'Name', 'eid', 'RuleClassName', 'ClassType', 'RuleType', 'RuleDisabled',
                    'LinkReference', 'InitialPhase', 'StartupRules', 'RulesOnly', 'ProcessRulesOnly',
                    'ConditionExpression', 'NotApplicable', 'PropertyKey', 'PostQuestionRules',
                    'ReadOnly', 'DisableInput', 'QuestionText', 'ActionCommand',
                    'DependencyType', 'DependentQuestions', 'CheckMandatoryFields',
                } or k in self.extra_metadata, encoding=self.encoding, handle=reader)
                if selected.truncated or selected.duplicates:
                    raise ValueError(f'Invalid/truncated structural metadata at {span.start}')
                node = TraceNode(f'{self.relative}@{span.start}', span.name, span.start, line,
                                 selected.values, stack[-1] if stack else None)
                if stack:
                    stack[-1].children.append(node)
                self.nodes.append(node)
                if node.meta.get('eid'):
                    self.eids.setdefault(node.meta['eid'], []).append(node)
                if not span.self_closing:
                    stack.append(node)
        if stack:
            raise ValueError('Unclosed structural tags')
        self.check_unchanged()

    def _restore(self, payload: dict) -> bool:
        try:
            raw_nodes = payload['nodes']
            if (not isinstance(raw_nodes, list) or len(raw_nodes) > self.max_nodes or
                    any(not isinstance(item, dict) for item in raw_nodes)):
                return False
            if payload['encoding'] != self.encoding.codec:
                return False
            nodes = []
            keys = set()
            for item in raw_nodes:
                key, tag, offset, line, meta = (item.get(k) for k in ('key', 'tag', 'offset', 'line', 'meta'))
                if (not isinstance(key, str) or not isinstance(tag, str) or tag not in
                        {'Project', 'Product', 'Phase', 'Rule', 'DataSource', 'Question', 'Button', 'ItemGroup'}
                        or not isinstance(offset, int) or isinstance(offset, bool)
                        or not 0 <= offset < self.before.st_size or offset % self.encoding.unit
                        or (nodes and offset <= nodes[-1].offset)
                        or key != f'{self.relative}@{offset}' or not isinstance(line, int)
                        or isinstance(line, bool) or line < 1 or not isinstance(meta, dict)
                        or any(not isinstance(k, str) or not isinstance(v, str) for k, v in meta.items())):
                    return False
                attrs = item.get('attrs')
                if attrs is not None and (not isinstance(attrs, dict) or
                                          any(not isinstance(k, str) or not isinstance(v, str) for k, v in attrs.items())):
                    return False
                if key in keys:
                    return False
                keys.add(key)
                nodes.append(TraceNode(key, tag, offset, line, meta, attrs=attrs))
            self.nodes = nodes
            by_key = {node.key: node for node in self.nodes}
            for item, node in zip(raw_nodes, self.nodes):
                parent = item.get('parent')
                children = item.get('children', [])
                if (parent is not None and (not isinstance(parent, str) or parent not in by_key)) or \
                        (not isinstance(children, list) or any(not isinstance(key, str) or key not in by_key for key in children)):
                    return False
                if parent is not None:
                    node.parent = by_key[parent]
                    # Parents precede children in the original byte stream;
                    # this also rejects cycles in linear time.
                    if node.parent.offset >= node.offset:
                        return False
                    node.parent.children.append(node)
            for item, node in zip(raw_nodes, self.nodes):
                if item.get('children', []) != [child.key for child in node.children]:
                    return False
            self.eids = {}
            for node in self.nodes:
                if node.meta.get('eid'):
                    self.eids.setdefault(node.meta['eid'], []).append(node)
            issues = payload['issues']
            node_issues = payload.get('node_issues', {})
            if not isinstance(issues, list) or not isinstance(node_issues, dict):
                return False
            if any(not isinstance(value, list) or any(not isinstance(issue, dict) for issue in value)
                   for value in node_issues.values()):
                return False
            if any(not isinstance(issue, dict) for issue in issues):
                return False
            self.issues = []
            if any(not isinstance(key, str) or key not in by_key for key in node_issues):
                return False
            if any(issue.get('node') != key or not isinstance(issue.get('code'), str)
                   for key, group in node_issues.items() for issue in group):
                return False
            self._cached_issues = dict(node_issues)
            return True
        except (KeyError, TypeError, ValueError, AttributeError):
            return False

    def save_cache(self) -> bool:
        if self.cache_dir is None:
            return False
        if self.cache_hit and not self._dirty:
            return True
        try:
            self.check_unchanged()
        except (OSError, ValueError):
            return False
        payload = {
            'encoding': self.encoding.codec,
            'nodes': [{'key': node.key, 'tag': node.tag, 'offset': node.offset, 'line': node.line,
                       'meta': node.meta, 'parent': node.parent.key if node.parent else None,
                       'children': [child.key for child in node.children], 'attrs': node.attrs}
                      for node in self.nodes],
            'eids': {eid: [node.key for node in nodes] for eid, nodes in self.eids.items()},
            'issues': self.issues,
            'node_issues': self._cached_issues,
        }
        result = save_cache(self.cache_dir, self._cache_key, file_fingerprint(self.path), payload)
        if result:
            self._dirty = False
        return result

    def check_unchanged(self):
        if file_fingerprint(self.path) != _fingerprint_from_stat(self.before):
            raise ValueError(f'File changed during trace: {self.relative}')

    def attributes(self, node: TraceNode) -> dict[str, str]:
        if self.cache_hit and node.attrs is not None:
            cached_issues = self._cached_issues.get(node.key)
            if cached_issues is not None and node.key not in self._replayed_issues:
                self.issues.extend(cached_issues)
                self._replayed_issues.add(node.key)
        if node.attrs is None:
            self.check_unchanged()
            span = find_tag_span(self.path, node.offset, encoding=self.encoding, containing=False)
            if span is None:
                raise ValueError(f'Missing tag at {node.offset}')
            read = read_tag_attributes(self.path, span, lambda _: True, encoding=self.encoding,
                                       max_value_bytes=1024 * 1024)
            node.attrs = read.values
            self._dirty = True
            generated = []
            for item in read.truncated:
                generated.append({'code': 'TRUNCATED_ATTRIBUTE', 'node': node.key, 'attribute': item.name})
            if read.duplicates:
                generated.append({'code': 'DUPLICATE_ATTRIBUTE', 'node': node.key, 'attributes': read.duplicates})
            if generated:
                self.issues.extend(generated)
                self._cached_issues[node.key] = generated
                self._replayed_issues.add(node.key)
        return node.attrs

    def release_attributes(self, node: TraceNode) -> None:
        # Keep valid attributes for reuse. Re-read diagnostic-bearing nodes on
        # subsequent activations, preserving the uncached diagnostic sequence.
        if self.cache_dir is None or node.key in self._cached_issues:
            node.attrs = None

    def phase_name(self, node: TraceNode) -> str:
        names = []
        while node:
            if node.tag in {'Product', 'Phase'}:
                names.append(node.meta.get('Name', ''))
            node = node.parent
        return '.'.join(reversed(names))

    def entry_roots(self, entry: str | None) -> list[TraceNode]:
        if entry and entry.startswith('@'):
            return self.eids.get(entry[1:], [])
        phases = [n for n in self.nodes if n.tag == 'Phase']
        if entry:
            return [n for n in phases if self.phase_name(n) == entry]
        return phases or [n for n in self.nodes if n.tag == 'Rule' and n.parent and n.parent.tag == 'Project']

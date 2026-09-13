"""Small, lazy structural indexes for files reached by a trace, never a database."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path

from .xmlbytes import detect_xml_encoding, iter_xml_visible_matches, find_tag_span, read_tag_attributes


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
    def __init__(self, root: Path, path: Path, max_nodes: int = 100_000, *, extra_metadata=()):
        self.path = path.resolve()
        self.relative = self.path.relative_to(root).as_posix()
        self.encoding = detect_xml_encoding(path)
        self.before = path.stat()
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
                } or k in extra_metadata, encoding=self.encoding, handle=reader)
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
        with path.open('rb') as handle:
            self.sha256 = hashlib.file_digest(handle, 'sha256').hexdigest()
        self.check_unchanged()

    def check_unchanged(self):
        now = self.path.stat()
        if (now.st_size, now.st_mtime_ns) != (self.before.st_size, self.before.st_mtime_ns):
            raise ValueError(f'File changed during trace: {self.relative}')

    def attributes(self, node: TraceNode) -> dict[str, str]:
        if node.attrs is None:
            self.check_unchanged()
            span = find_tag_span(self.path, node.offset, encoding=self.encoding, containing=False)
            if span is None:
                raise ValueError(f'Missing tag at {node.offset}')
            read = read_tag_attributes(self.path, span, lambda _: True, encoding=self.encoding,
                                       max_value_bytes=1024 * 1024)
            node.attrs = read.values
            for item in read.truncated:
                self.issues.append({'code': 'TRUNCATED_ATTRIBUTE', 'node': node.key, 'attribute': item.name})
            if read.duplicates:
                self.issues.append({'code': 'DUPLICATE_ATTRIBUTE', 'node': node.key, 'attributes': read.duplicates})
        return node.attrs

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

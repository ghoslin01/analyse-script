"""Bounded evidence for Temenos $%IF templates; never render runtime values."""
from __future__ import annotations

import re


REF = re.compile(r'\$\$((?:[^$\r\n]|\$\$[^$\r\n]+\$)+)\$')
DIRECTIVE = re.compile(r'\$%(IF\b[^$]*|ELSE|ENDIF)\$', re.I)
BARE_FIELD = re.compile(r'!?[A-Za-z_]\w*(?:\[[^\]\r\n]+\])?(?:\.[A-Za-z_]\w*(?:\[[^\]\r\n]+\])?)+')


def field_references(expression: str) -> list[str]:
    result = []
    for match in REF.finditer(expression):
        value = match.group(1).strip()
        value = re.sub(r'\.[A-Za-z_]\w*\(.*$', '', value)
        if '$$' in value:
            result.extend(p for p in field_references(value) if p not in result)
        if value and value not in result:
            result.append(value)
    return result


def condition_references(expression: str) -> list[str]:
    result = field_references(expression)
    # Plain datastore paths in IF predicates are not $$ substitutions.
    unquoted = re.sub(r"'[^']*'|\"[^\"]*\"", '', expression)
    unquoted = REF.sub('', unquoted)
    for match in BARE_FIELD.finditer(unquoted):
        value = match.group()
        if unquoted[match.end():].lstrip().startswith('('):
            value = value.rsplit('.', 1)[0]
        if value and value not in result:
            result.append(value)
    return result


def template_evidence(value: str, *, variants: bool = False, max_variants: int = 32) -> dict:
    """Preserve raw text, nested branch guards, and conditional dependencies.

    IF predicate semantics and substitutions (including date functions) stay
    symbolic. Variants enumerate syntax branches, not proven runtime URLs.
    """
    result = {'raw': value, 'status': 'parsed', 'tree': [], 'dependencies': [], 'variants': []}
    if len(value.encode('utf-8')) > 128 * 1024:
        result.update(status='limit', reason='template_bytes')
        return result
    root, stack, current = [], [], None
    current = root
    cursor, count = 0, 0
    try:
        for hit in DIRECTIVE.finditer(value):
            text = value[cursor:hit.start()]
            if '$%' in text:
                raise ValueError('Unsupported or incomplete template directive')
            if text:
                current.append({'type': 'text', 'value': text})
            directive = hit.group(1)
            if directive.upper().startswith('IF'):
                expression = directive[2:].strip()
                if not expression:
                    raise ValueError('Empty IF condition')
                node = {'type': 'if', 'condition': expression, 'then': [], 'else': [], 'offset': hit.start()}
                current.append(node)
                stack.append((current, node, False))
                current = node['then']
            elif directive.upper() == 'ELSE':
                if not stack or stack[-1][2]:
                    raise ValueError('Unexpected or duplicate ELSE')
                parent, node, _ = stack[-1]
                stack[-1] = (parent, node, True)
                current = node['else']
            else:
                if not stack:
                    raise ValueError('Unexpected ENDIF')
                current, _, _ = stack.pop()
            cursor, count = hit.end(), count + 1
            if len(stack) > 32 or count > 512:
                raise ValueError('Template structure limit exceeded')
        tail = value[cursor:]
        if '$%' in tail or stack:
            raise ValueError('Unclosed or unsupported template directive')
        if tail:
            current.append({'type': 'text', 'value': tail})
    except ValueError as error:
        # No complete-looking alternatives from a partially parsed template.
        result.update(status='unresolved', reason=str(error))
        result['dependencies'] = [{'field': p, 'role': 'unresolved', 'guards': []}
                                  for p in condition_references(value)]
        return result
    result['tree'] = root

    def visit(nodes, guards):
        for node in nodes:
            if node['type'] == 'text':
                paths, role, expression = field_references(node['value']), 'substitution', node['value']
            else:
                paths, role, expression = condition_references(node['condition']), 'condition', node['condition']
                for branch, children in ((True, node['then']), (False, node['else'])):
                    visit(children, [*guards, {'condition': expression, 'branch': branch, 'offset': node['offset']}])
            for path in paths:
                item = {'field': path, 'role': role, 'guards': guards, 'expression': expression}
                if item not in result['dependencies']:
                    result['dependencies'].append(item)
    visit(root, [])
    if variants:
        def expand(nodes):
            candidates = [{'template': '', 'guards': []}]
            for node in nodes:
                if node['type'] == 'text':
                    additions = [{'template': node['value'], 'guards': []}]
                else:
                    additions = []
                    for branch, children in ((True, node['then']), (False, node['else'])):
                        for child in expand(children):
                            additions.append({'template': child['template'], 'guards': [
                                {'condition': node['condition'], 'branch': branch, 'offset': node['offset']}, *child['guards']]})
                if len(candidates) * len(additions) > max_variants:
                    raise ValueError('path_variant_limit')
                candidates = [{'template': a['template'] + b['template'], 'guards': a['guards'] + b['guards']}
                              for a in candidates for b in additions]
            return candidates
        try:
            result['variants'] = expand(root)
        except ValueError as error:
            result.update(status='limit', reason=str(error))
    return result


def template_markdown(label: str, evidence: dict) -> list[str]:
    """Show symbolic alternatives/fragments without silently normalizing URL text."""
    def inline(value):
        return value.replace('\n', r'\n').replace('\r', r'\r').replace('`', r'\`')

    def guards_text(guards):
        return '; '.join(f"({g['condition']}) = {str(g['branch']).lower()}" for g in guards) or 'unconditional fragment'

    lines = [f"Template `{label}`: **{evidence['status']}** (symbolic; not a resolved request)", '',
             '```text', evidence['raw'], '```', '']
    if evidence.get('reason'):
        lines += [f"Boundary: `{evidence['reason']}`", '']
    for variant in evidence['variants']:
        lines.append(f"- Path candidate: `{inline(variant['template'])}`; when {guards_text(variant['guards'])}")
    if not evidence['variants']:
        def fragments(nodes, guards):
            for node in nodes:
                if node['type'] == 'text':
                    if node['value'].strip():
                        lines.append(f"- Fragment: `{inline(node['value'])}`; when {guards_text(guards)}")
                else:
                    for branch, children in ((True, node['then']), (False, node['else'])):
                        fragments(children, [*guards, {'condition': node['condition'], 'branch': branch}])
        fragments(evidence['tree'], [])
    for dependency in evidence['dependencies']:
        lines.append(f"- Reads `{dependency['field']}` ({dependency['role']}); when {guards_text(dependency['guards'])}")
    return [*lines, '']

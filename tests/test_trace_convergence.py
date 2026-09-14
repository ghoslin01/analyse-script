from ifp_contract.trace import Trace


def field(path, group=False, scope='root'):
    return {'scope': scope, 'path': path, 'group': group}


def event(number, *, writes=(), reads=(), kind='set', parents=(), guards=()):
    return {
        'id': f'e{number}',
        'sequence': number,
        'node': f'n{number}',
        'kind': kind,
        'scope': 'root',
        'guards': list(guards),
        'loops': [],
        'parents': list(parents),
        'triggers': [],
        'writes': list(writes),
        'reads': list(reads),
        'entry': 'P.Input',
        'name': f'event {number}',
    }


def make_trace(tmp_path, events):
    trace = Trace(tmp_path)
    trace.events = events
    trace.nodes = {
        e['node']: {'file': 'A.ifp', 'line': e['sequence'] + 1}
        for e in events
    }
    return trace


def test_opaque_boundary_is_retained_without_expanding_declared_reads(tmp_path):
    events = [event(i, writes=[field(f'Unrelated{i}')]) for i in range(100)]
    events += [event(100 + i, kind='unknown', reads=[field(f'Unrelated{i}')])
               for i in range(100)]
    events += [event(200, writes=[field('Target')]),
               event(201, reads=[field('Target')], writes=[field('Result')])]
    data = make_trace(tmp_path, events).slice(['e201'], 'Result')
    ids = {e['id'] for e in data['events']}
    assert ids == {f'e{i}' for i in range(100, 202)}
    assert [(edge['from'], edge['to']) for edge in data['edges']] == [('e200', 'e201')]
    assert all(e['slice_role'] == 'context_or_boundary' for e in data['events'][:100])
    assert data['conclusions']['overall_status'] == 'blocked'
    assert len(data['opaque_dependencies']) == 100


def test_structural_parent_does_not_propagate_reads_but_real_guard_does(tmp_path):
    unrelated = field('Unrelated.Value')
    events = [
        event(0, writes=[unrelated]),
        event(1, reads=[unrelated]),
        event(2, parents=['e1'], writes=[field('Target')], reads=[]),
    ]
    data = make_trace(tmp_path, events).slice(['e2'], 'Target')
    ids = {e['id'] for e in data['events']}
    assert {'e1', 'e2'} <= ids
    assert 'e0' not in ids
    assert not any(edge['field']['path'] == 'Unrelated.Value'
                   for edge in data['edges'])

    condition = field('Condition.Value')
    guarded_events = [
        event(0, writes=[condition]),
        event(1, kind='evaluate', reads=[condition]),
        event(2, parents=['e1'], guards=[{'event': 'e1', 'branch': 'True'}],
              writes=[field('Target')]),
    ]
    guarded = make_trace(tmp_path, guarded_events).slice(['e2'], 'Target')
    guarded_ids = {e['id'] for e in guarded['events']}
    assert {'e0', 'e1', 'e2'} <= guarded_ids
    assert any(edge['from'] == 'e0' and edge['to'] == 'e1'
               and edge['field']['path'] == 'Condition.Value'
               for edge in guarded['edges'])


def test_group_mapping_projects_only_the_requested_instance_and_field(tmp_path):
    events = [
        event(0, writes=[field('Source[1].Wanted')]),
        event(1, writes=[field('Source[1].Other')]),
        event(2, writes=[field('Source[2].Wanted')]),
        event(3, kind='output_mapping',
              reads=[field('Source[A]', group=True)],
              writes=[field('Target[A]', group=True)]),
    ]
    data = make_trace(tmp_path, events).slice(['e3'], 'Target[1].Wanted')

    source_edges = [edge for edge in data['edges'] if edge['to'] == 'e3']
    assert [(edge['from'], edge['field']['path']) for edge in source_edges] == [
        ('e0', 'Source[1].Wanted'),
    ]
    assert {e['id'] for e in data['events']} == {'e0', 'e3'}

    # Asking for the whole group must still include every requested member.
    whole = make_trace(tmp_path, events).slice(['e3'], 'Target[1]')
    assert {e['id'] for e in whole['events']} == {'e0', 'e1', 'e3'}


def test_opaque_declared_data_read_is_followed_while_opaque_boundary_is_not(tmp_path):
    known = field('Known.Value')
    unrelated = field('Unrelated.Value')
    events = [
        event(0, writes=[unrelated]),
        event(1, writes=[known]),
        event(2, kind='unknown', writes=[field('Target')], reads=[known]),
        event(3, kind='unknown', reads=[unrelated]),
        event(4, reads=[field('Target')], writes=[field('Result')]),
    ]

    data = make_trace(tmp_path, events).slice(['e4'], 'Result')

    ids = {e['id'] for e in data['events']}
    assert ids == {'e1', 'e2', 'e3', 'e4'}
    assert any(edge['from'] == 'e1' and edge['to'] == 'e2'
               and edge['field']['path'] == 'Known.Value'
               for edge in data['edges'])
    assert not any(edge['field']['path'] == 'Unrelated.Value'
                   for edge in data['edges'])

"""Candidate indexes for a collected trace; precise overlap checks stay in trace."""
from collections import defaultdict


class TraceIndex:
    def __init__(self, events, shape):
        self.events = events
        self.shape = shape
        self.exact = defaultdict(set)
        self.groups = defaultdict(set)
        self.descendants = defaultdict(set)
        self.instances = defaultdict(list)
        self.opaque = defaultdict(list)
        self.guards = {}
        for index, event in enumerate(events):
            self.guards[event['id']] = {g['event']: g['branch'] for g in event['guards']}
            if event['kind'] == 'instance':
                self.instances[event['scope']].append(event)
            elif event['kind'] in {'unknown', 'goto'}:
                self.opaque[event['scope']].append(event)
            for written in event['writes']:
                scope, path = written['scope'], shape(written['path'])
                self.exact[scope, path].add(index)
                if written.get('group'):
                    self.groups[scope, path].add(index)
                # Index prefixes, not array instances. overlaps() still checks
                # concrete indices and symbolic groups on the small candidate set.
                for end, char in enumerate(path):
                    if char == '.':
                        self.descendants[scope, path[:end]].add(index)

    def candidates(self, read):
        scope, path = read['scope'], self.shape(read['path'])
        matches = set(self.exact.get((scope, path), ()))
        for end, char in enumerate(path):
            if char == '.':
                matches.update(self.groups.get((scope, path[:end]), ()))
        if read.get('group'):
            matches.update(self.descendants.get((scope, path), ()))
        # Preserve source collection order, including cross-UI-event candidates.
        return [self.events[index] for index in sorted(matches)]

    def compatible(self, writer, reader):
        constraints = self.guards[reader['id']]
        return not any(g['event'] in constraints and constraints[g['event']] != g['branch']
                       for g in writer['guards'])

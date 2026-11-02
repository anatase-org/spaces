"""Read compact SCU trees in test/probe assertions, without exposing host rows."""
import json
import re

ROW = re.compile(r'^( *)(?:\[(\d+)\] )?(.*?) (null|"(?:[^"\\]|\\.)*")(.*)$')
DECODER = json.JSONDecoder()


def tree_rows(text):
    rows, parents = [], []
    for line in text.splitlines():
        match = ROW.fullmatch(line)
        if not match:
            raise ValueError(f"Invalid SCU tree line: {line!r}")
        indent, element_id, role, label, rest = match.groups()
        depth = len(indent) // 2
        if depth > len(parents):
            raise ValueError("SCU tree skips a parent level")
        parents = parents[:depth]
        row = {"element_index": len(rows),
               "parent_index": parents[-1] if parents else -1, "depth": depth,
               "role": role, "label": json.loads(label), "enabled": True, "frame": None}
        if element_id is not None:
            row["element_id"] = int(element_id)
        while rest.strip():
            rest = rest.lstrip()
            field = re.match(r'(value|minimum|maximum|frame)=', rest)
            if field:
                value, end = DECODER.raw_decode(rest[field.end():])
                rest = rest[field.end() + end:]
                row[field[1]] = dict(zip(("x", "y", "width", "height"), value)) if field[1] == "frame" else value
            else:
                word, _, rest = rest.partition(" ")
                if word in ("type_text", "set_value"):
                    row.setdefault("actions", []).append(word)
                elif word == "disabled":
                    row["enabled"] = False
                elif word in ("focused", "selected", "checked", "protected", "text_truncated"):
                    row[word] = True
                else:
                    raise ValueError(f"Unknown SCU tree field: {word}")
        rows.append(row)
        parents.append(row["element_index"])
    return rows


def read_result(response):
    """Decode the actual readable MCP response for test/probe assertions.

    This is test-side parsing, never additional data sent to the model.
    """
    result, observation, app = {}, None, None
    tree_lines = []

    def finish_tree():
        if observation is not None and tree_lines:
            observation['tree_markdown'] = '\n'.join(tree_lines) + '\n'
            observation['elements'] = tree_rows(observation['tree_markdown'])
            tree_lines.clear()

    for block in response.get('content', []):
        if block.get('type') != 'text':
            continue
        for line in block['text'].splitlines():
            if not line or line.startswith('The screen is locked.'):
                continue
            if line.startswith('Screen: '):
                result.update(locked=line == 'Screen: locked', apps=[])
            elif match := re.fullmatch(r'Canvas: (\d+) x (\d+); id: (\d+)', line):
                width, height, capture = map(int, match.groups())
                result['canvas'] = dict(width=width, height=height, id=capture)
            elif line == 'Windows:':
                continue
            elif 'apps' in result and line.startswith('  ') and not line.startswith('    '):
                app = dict(app_name=json.loads(line[2:-1]), windows=[])
                result['apps'].append(app)
            elif 'apps' in result and line.startswith('    window_id='):
                match = re.match(r'    window_id=(\d+) ', line)
                title, end = DECODER.raw_decode(line[match.end():])
                rest = line[match.end() + end:]
                bounds = json.loads(rest.split(' bounds=', 1)[1])
                app['windows'].append(dict(window_id=int(match[1]), title=title,
                    active=' active' in rest, shown=' hidden' not in rest,
                    bounds=dict(zip(('x', 'y', 'width', 'height'), bounds))))
            elif match := re.fullmatch(r'Status: (\w+); completed: (\d+)', line):
                result.update(status=match[1], completed=int(match[2]), observations=[])
            elif match := re.fullmatch(r'(Window (\d+)|Desktop)(?:; id: (\d+))?(?:; (\d+) x (\d+))?', line):
                finish_tree()
                observation = {}
                if match[2] is not None:
                    observation['window_id'] = int(match[2])
                if match[3] is not None:
                    observation['id'] = int(match[3])
                if match[4] is not None:
                    observation.update(width=int(match[4]), height=int(match[5]))
                result['observations'].append(observation)
            elif match := re.fullmatch(r'Screenshot: (\d+) x (\d+); bounds=(.*)', line):
                observation['screenshot'] = dict(width=int(match[1]), height=int(match[2]),
                    bounds=dict(zip(('x', 'y', 'width', 'height'), json.loads(match[3]))))
            elif match := re.fullmatch(r'(Error|Failed|Accessibility|Screenshot): (\w+)(?: \(step (\d+), ([^)]+)\))?(?:: (.*))?', line):
                error = dict(code=match[2], hint=match[5] or '')
                if match[3] is not None:
                    error.update(index=int(match[3]), tool=match[4])
                if match[1] == 'Failed':
                    result['failed'] = error
                elif observation is None:
                    result.update(error)
                else:
                    field = {'Error': 'error', 'Accessibility': 'accessibility_error',
                             'Screenshot': 'screenshot_error'}[match[1]]
                    observation[field] = error
            elif match := re.fullmatch(r'Warning \(step (\d+), ([^)]+)\): (.*)', line):
                result.setdefault('warnings', []).append(dict(index=int(match[1]), tool=match[2], warning=match[3]))
            elif line.startswith('Tree truncated;'):
                observation['truncated'] = True
            elif line == 'Full tree.':
                observation['tree_markdown'] = ''
                observation['elements'] = []
            elif line == 'No Changes.':
                observation['tree_diff'] = 'No Changes.\n'
            elif line.startswith(('@@ ', '+', '-')) and observation is not None:
                observation['tree_diff'] = observation.get('tree_diff', '') + line + '\n'
            elif observation is not None:
                if ROW.fullmatch(line) is None:
                    raise ValueError(f'Unknown SCU response line: {line!r}')
                observation.setdefault('truncated', False)
                tree_lines.append(line)
            else:
                raise ValueError(f'Unknown SCU response line: {line!r}')
    finish_tree()
    return result

"""Read compact SCU trees in test/probe assertions, without exposing host rows."""
from copy import deepcopy
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


def read_trees(metadata):
    """Derive rows only for local assertions; saved/wire responses stay compact."""
    result = deepcopy(metadata)
    for observation in result.get("observations", []):
        if "tree_markdown" in observation:
            observation["elements"] = tree_rows(observation["tree_markdown"])
    return result

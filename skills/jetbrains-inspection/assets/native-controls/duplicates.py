def collect_labels(groups):
    result = {}
    for category, entries in groups.items():
        labels = []
        for entry in entries:
            label = entry.strip().casefold()
            if label and label not in labels:
                labels.append(label)
        labels.sort()
        result[category] = tuple(labels)
    keys = sorted(result)
    return [(key, result[key]) for key in keys]


def collect_titles(groups):
    result = {}
    for category, entries in groups.items():
        labels = []
        for entry in entries:
            label = entry.strip().casefold()
            if label and label not in labels:
                labels.append(label)
        labels.sort()
        result[category] = tuple(labels)
    keys = sorted(result)
    return [(key, result[key]) for key in keys]

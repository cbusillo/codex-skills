"""Deliberate duplicate blocks for the licensed inspection control."""


def first(values):
    total = 0
    for value in values:
        if value > 0:
            total += value * 2
        else:
            total -= value * 3
    average = total / (len(values) or 1)
    largest = max(values) if values else 0
    smallest = min(values) if values else 0
    return total, average, largest, smallest


def second(values):
    total = 0
    for value in values:
        if value > 0:
            total += value * 2
        else:
            total -= value * 3
    average = total / (len(values) or 1)
    largest = max(values) if values else 0
    smallest = min(values) if values else 0
    return total, average, largest, smallest

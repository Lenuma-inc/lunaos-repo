"""Pure parsers for the binutils data used by the ABI rebuild scan."""

import re

ELF64_POINTER_SIZE = 8

RUNTIME_PATHS = {
    "python": r"(?:usr/)?lib/python(3\.\d+)/",
    "perl": r"(?:usr/)?lib/perl5/(?:(?:site|vendor|core)_perl/)?(5\.\d+)/",
    "ruby": r"(?:usr/)?lib/ruby/(?:(?:gems|site_ruby)/)?(\d+\.\d+)(?:\.\d+)?/",
    "ghc": r"(?:usr/)?lib/(?:ghc-?|ghc/)(\d+\.\d+\.\d+)/",
}


def runtime_paths(names):
    result = {}
    for name in names:
        if name.endswith("/"):
            continue
        for runtime, pattern in RUNTIME_PATHS.items():
            if match := re.match(pattern, name):
                result.setdefault(runtime, set()).add(match[1])
        if match := re.search(r"\.cpython-3(\d+)(?:-[^/]+)?\.so$", name):
            result.setdefault("python", set()).add("3." + match[1])
    return result


def version_nodes(output):
    defined, needed = set(), {}
    section, library = None, None
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("Version "):
            section = "defined" if line.startswith("Version definition") else "needed" if line.startswith("Version needs") else None
            library = None
        if section == "needed" and (match := re.search(r"File:\s+(\S+)", line)):
            library = match[1]
        if match := re.search(r"Name:\s+(\S+)", line):
            if section == "defined" and not re.search(r"Flags:\s*BASE", line):
                defined.add(match[1])
            elif section == "needed" and library:
                needed.setdefault(library, set()).add(match[1])
    return defined, needed


def vtable_slots(symbols, relocations):
    slots = []
    for line in relocations.splitlines():
        fields = line.split()
        if len(fields) >= 5 and fields[2] == "R_X86_64_64":
            slots.append((int(fields[0], 16), fields[4].split("@", 1)[0]))
    slots.sort()
    tables = {}
    for line in symbols.splitlines():
        fields = line.split()
        if len(fields) == 4 and fields[3].startswith("_ZTV"):
            address, size = int(fields[0], 16), int(fields[1], 16)
            targets = [None] * (size // ELF64_POINTER_SIZE)
            for offset, symbol in slots:
                if address <= offset < address + size and not symbol.startswith("_ZTI"):
                    targets[(offset - address) // ELF64_POINTER_SIZE] = symbol
            tables[fields[3].split("@", 1)[0]] = targets
    return tables


def changed_vtables(old, new, imports):
    """Detect moved or removed imported slots; unnamed relocations remain unknown."""
    required = {symbol.split("@", 1)[0] for symbol in imports}
    broken = []
    for table, slots in old.items():
        current = new.get(table, [])
        if any(slot in required and not slot.startswith(("__cxa_", "_Unwind_"))
               and slot != "__gxx_personality_v0"
               and (i >= len(current) or current[i] is not None and current[i] != slot)
               for i, slot in enumerate(slots)):
            broken.append(table)
    return broken

## 1. <!-- Task Group Name -->

- [ ] 1.1 <!-- Task description -->
- [ ] 1.2 <!-- Task description -->

## 2. <!-- Task Group Name -->

- [ ] 2.1 <!-- Task description -->
- [ ] 2.2 <!-- Task description -->

## Task Dependency Graph
```json
{
    "waves": [
        { "id": 0, "dependsOn": [],    "tasks": ["1.1", "4.1"] },
        { "id": 1, "dependsOn": [0],   "tasks": ["1.2", "1.3", "1.4"] },
        { "id": 2, "dependsOn": [0],   "tasks": ["2.1", "4.2", "4.3"] },
        { "id": 3, "dependsOn": [1,2], "tasks": ["2.2", "2.3"] },
        { "id": 4, "dependsOn": [3],   "tasks": ["5.1", "5.2", "5.3"] }
    ]
}
```

Waves whose `dependsOn` prerequisites are all satisfied run
**concurrently**, one implementer sub-agent per wave. Wave id is an
identifier, not a wall-clock ordering. Tests SHIP IN THE SAME WAVE
as the code they cover; no two waves may edit the same file.

`##` headings are organizational, not wave boundaries — wave membership
is driven by file ownership + shared test surface (note wave 0 above
spans headings 1 and 4). To parallelize, split file-disjoint independent
work into **sibling waves** sharing a `dependsOn`, not into multiple
tasks inside one wave; but keep tasks that share a file or test together.

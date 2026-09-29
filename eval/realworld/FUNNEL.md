# Real-world benchmark: how the incidents were selected

Every revert in each repository, filtered by the rules in `build.py` and taken newest-first until the per-repo cap. Nothing was hand-picked. Counts are cumulative: each column is what survived every rule to its left.

| Repo | Reverts | Names its target | Target exists | Not a merge | Target not a revert | Is itself a revert | Reverted in 90m-7d | States a reason | Describes a symptom | Doesn't name the answer | Landed in the window | Window >= 10 commits | Not a duplicate | Selected |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| prometheus | 58 | 58 | 57 | 55 | 47 | 40 | 19 | 5 | 2 | 2 | 1 | 1 | 1 | **1** |
| moby | 184 | 184 | 183 | 178 | 169 | 150 | 59 | 10 | 4 | 4 | 3 | 3 | 3 | **3** |
| grafana | 686 | 686 | 357 | 355 | 322 | 295 | 190 | 3 | 2 | 2 | 1 | 1 | 1 | **1** |
| go | 308 | 278 | 79 | 79 | 77 | 75 | 44 | 27 | 15 | 15 | 10 | 10 | 10 | **10** |
| etcd | 41 | 41 | 38 | 34 | 32 | 31 | 11 | 1 | 0 | 0 | 0 | 0 | 0 | **0** |
| containerd | 48 | 48 | 48 | 46 | 45 | 43 | 18 | 0 | 0 | 0 | 0 | 0 | 0 | **0** |
| consul | 127 | 127 | 108 | 104 | 103 | 96 | 74 | 0 | 0 | 0 | 0 | 0 | 0 | **0** |

The funnel stops early once a repo reaches its cap, so the middle columns count only the reverts examined before then, not every revert in the repo.

## Rejected for leaking the answer

Alerts whose cleaned text still contained the guilty commit's subject line, or named its author.

None.

## Selected

| Incident | Commits in window | Hours to revert | Alert title |
| --- | ---: | ---: | --- |
| RW-prometheus-01 | 22 | 16.1 | Not only do we know this to produce problematic artifacts, it refuses |
| RW-moby-01 | 41 | 24.3 | caused some issues in the docker/cli formatting code that needs some |
| RW-moby-02 | 49 | 28.6 | turns out to break other test cases/the registry flow. |
| RW-moby-03 | 56 | 5.8 | Commit caused issues on systems with case-insensitive filesystems. |
| RW-grafana-01 | 42 | 3.6 | Goavro doesn't compile on 32-bit systems. |
| RW-go-01 | 51 | 67.6 | Breaks some tests inside Google (on arm64?) |
| RW-go-02 | 154 | 21.4 | breaks viewing documentation for unfetched modules |
| RW-go-03 | 14 | 13.0 | cockroachdb benchmark failing. Likely due to . |
| RW-go-04 | 29 | 15.4 | This breaks toolchain bootstrapping in Google. Root cause investigation is pending. |
| RW-go-05 | 27 | 20.6 | Causes failures on big endian platforms and riscv64. |
| RW-go-06 | 40 | 45.8 | TESTL rule is wrong when the result is used for an ordered comparison. |
| RW-go-07 | 34 | 45.2 | aka |
| RW-go-08 | 128 | 3.4 | this is causing some bootstrap build problems with older versions of Go 1.17, as I underst |
| RW-go-09 | 25 | 1.8 | breaks the longtest builders for x/tools |
| RW-go-10 | 61 | 23.2 | Bug somewhere in this code, causing wasm and maybe linux/386 to fail. |

# Recorded forge responses

One directory per adapter, written by `scripts/probe_forge.py --record` while it ran the adapter against a real
public repository: `forgejo/` from codeberg.org/forgejo/forgejo, `gitlab/` from gitlab.com/gitlab-org/cli, and
`ado/` from dev.azure.com/powershell/PowerShell/PSGallery-Cache-Repo. `tests/test_forges.py` replays them in
place of `Rest.call`, so the adapters' URL building and normalization are tested offline against what the
forges actually said.

Each `index.json` lists every request in order, with the file holding its response or the error the transport
raised (a 401 for members on gitlab.com anonymously, a sign-in page for Azure DevOps branch policies). When a
URL was asked for more than once (a rate limit, then the retry), the last answer is the one replayed.

The recordings hold nothing about whoever made them: only the response headers the adapters read are kept,
the request that answers "who am I" is never recorded, and Azure DevOps's "The user '<id>'" refusals are
scrubbed. Two oversized responses were cut to a representative slice (a thousand reruns of one pipeline, and
the fields of a tree listing the adapter never reads); everything else is verbatim. Re-record with the script
when an adapter asks for something new, then refresh the expectations in the tests, since the data is
whatever the forge said that day.

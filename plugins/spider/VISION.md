# Spider: the vision

Spider is a starting point, not a product. This page says what it's for, who it's for, what it won't grow into,
and how to make it your own. The [README](README.md) says how it works today.

## What it's for

Keeping up with AI means a lot of reading. Twice a day, Spider gathers what changed in AI technology and news since
its last post: new models, dev-tool releases, breakout papers, big business moves and new videos from creators you
follow. One Claude session judges it against a written taste, every link is checked, and a short digest goes to
Slack.

Plenty of tools like this exist. This one is shared as a worked example: one person's answer to how to pick
sources, decide what matters and keep the result honest, published so others can see how it was done, learn from
it, and feel safe sharing what they've built too.

## Who it's for

People who build with AI tools and want a working example they can read, run and change, including people who are
new to this and learning in the open. It was built and tuned for one reader, an engineer who builds with Claude
Code every day. The shipped `taste.md` is that reader's, kept as an example.

## Good enough to share

Spider is good enough to share, not perfect. It runs, it's in daily use, and its rough edges are listed in the
[repo's README](../../README.md). A fix belongs here when someone using Spider would actually run into the
problem. Hardening, polish and generality are follow-ups, not the bar.

## What it isn't

| Spider is not | It is |
|---|---|
| A product or a hosted service | A scheduled job that runs on your own Mac |
| A general news feed | AI technology and news, and nothing else |
| A community project | Written and maintained by one owner |
| A mirror of the owner's own copy | A snapshot that doesn't follow the owner's copy as it grows |

## Make it your own

Fork it or clone it; that's what it's here for, and it's MIT-licensed. Then:

- Rewrite `taste.md`. It's what the judge scores against, and the shipped one belongs to someone else.
- Change the creators in `creators.json` and the feeds in `sources.json`.
- Set your own times in `schedule.json`.

Your fork is yours to keep and maintain. This repo may change or be taken down.

## Direction

The owner's own copy is heading toward learning from reader feedback and toward publishing its digest as a feed
that other agents can read, in Slack, Teams or elsewhere. None of that is built here, and it reaches this repo only
as a later snapshot, if at all.

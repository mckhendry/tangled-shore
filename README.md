# tangled-shore

A Claude Code plugin marketplace for keeping up with AI technology and news. Its first plugin, Spider, posts a
digest of what changed in AI to a Slack channel twice a day: new models, dev-tool releases, breakout papers, big
business moves and new videos from the creators you follow, each with a checked link.

## Install

```
/plugin marketplace add mckhendry/tangled-shore
/plugin install spider@tangled-shore
```

Spider is a scheduled job, not a skill, so installing the plugin only fetches its files. Its setup (a Slack app,
a Keychain item, one login and a settings file) is in [`plugins/spider/README.md`](plugins/spider/README.md).

## What a run does

At each slot, Spider:

1. pulls lab feeds, GitHub releases, Hacker News, Hugging Face daily papers and YouTube creators, with no model;
2. runs one `claude -p` session that searches the web for what the feeds miss, groups the same event, drops what
   it already posted, and scores the rest against a taste profile;
3. checks every link answers, or was listed by its own publisher's feed this run, and drops any it already posted;
4. posts up to 8 items to Slack, each marked shipped or announced, and records them only after Slack says ok.

The rubric: models and labs first, then dev tools, breakout papers, big business moves and new videos.
Popularity is evidence, not importance, and announced is not shipped. The full rubric and the main limits are in
the plugin's README.

## What's rough

- macOS only: it runs under launchd and reads its token from the Keychain.
- Slack only: there's no other place to post yet.
- The judge (one Sonnet 5.5 session) needs a one-time login for its own blank Claude profile
  (`~/.claude-clean`), covered in setup.

## Forks and clones

Forks and clones are welcome. This repo is here for people to read and learn from, so take what's useful. Issues
are off, and the only contributor is the owner.

## The names

The names come from Destiny. The Tangled Shore is a lawless reef where the Spider, a Fallen crime lord, runs a
network of informants and trades in what they bring him. An agent that goes out every morning and afternoon to
gather what happened, then reports back, seemed to belong there.

## License

MIT. See [`LICENSE`](LICENSE).

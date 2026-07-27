# Code of Conduct

This project is infrastructure. It runs on the request path of other people's
production services, and the only thing we are here to do is make it correct,
cheap, and safe to run. This document covers conduct in issues, pull requests,
reviews, and every other project space.

It is short on purpose. Two principles cover nearly everything.

## 1. Judge the argument, not the author

Technical claims stand or fall on evidence and reasoning. Who is making the
claim is irrelevant to whether it is true.

- Bring evidence. For a library like this one that means benchmarks, profiles,
  reproductions, scrape output, or a citation to the OpenMetrics or
  OpenTelemetry spec. An assertion with no support is a starting point for a
  discussion, not the end of one.
- Seniority, tenure, employer, and popularity are not arguments. Neither is
  "this is how we have always done it."
- Say "I don't know" when you don't. It is far cheaper than a confident guess
  that someone else has to debug in production six months from now.
- Update your position when the evidence changes, and do it out loud. Being
  publicly persuaded is a display of competence, not a loss.
- Attack the idea as hard as you like. Rigorous review is a service to the
  person receiving it. Do not attack the person holding the idea.
- Assume the other party is competent and arguing in good faith until they
  demonstrate otherwise. Most disagreements here are missing context, not
  malice.

If you are on the receiving end of a hard review, read it for the technical
content. Directness is not hostility.

## 2. Identity and politics are off topic

We do not need to know anything about you beyond the quality of your reasoning
and the strength of your character. Contributions are evaluated on the code and
the argument behind it, and on nothing else.

- Keep political, ideological, and religious advocacy out of project spaces.
- Do not invoke your own identity, or anyone else's, as an argument. It carries
  no evidentiary weight, for you or against you.
- Do not speculate about a contributor's identity, background, or motives. If
  you think a patch is wrong, say why it is wrong.

To be unambiguous about what this means: it is not that some people are
unwelcome here. It is that none of these attributes are relevant to the work,
which means none of them can be grounds for advantage or for exclusion. Show up
able to think clearly and write good code and you are welcome, and nothing else
about you will be held for or against you.

## Conduct that gets you removed

- Harassment, threats, or sustained personal attacks.
- Publishing anyone's private information without permission.
- Arguing in deliberate bad faith: misrepresenting someone's position,
  manufacturing evidence, or reopening a settled question without new
  information in order to exhaust the other side.
- Persistently derailing a thread after being asked to stop.
- Knowingly contributing malicious code, a compromised dependency, or telemetry
  that exfiltrates data from a deploying service. Given where this library
  sits, this is treated as a security incident and not as a conduct dispute.

## Enforcement

Maintainers will edit or remove contributions that violate the above, and will
lock threads or revoke access when it is warranted. The usual first response is
a direct request to change course; removal is for people who do not.

Report problems to spencer@spencercmd.com. Reports will be handled discreetly
and the reporter's identity kept confidential. Reasoning for a moderation
decision will be given where it is practical to do so, and maintainer judgment
is final.

# do-llm-reviews-improve-code

Does an LLM reviewer make an LLM builder's code better? A pre-registered replay of real review-caught defects.

> **Status:** work in progress. The first run (v1) is in [`eval/results/replay-report.md`](eval/results/replay-report.md): review helped on defects but failed its harm guards, so its verdict is no effect. A corrected v2 is in preparation.

## Run

Python 3, standard library only.

```bash
python3 -m unittest discover -s eval/tests -t .    # expect: OK
python3 -m eval report --experiment replay         # needs the run records in eval/data/
```

Structure: [ARCHITECTURE.md](ARCHITECTURE.md).

## License

MIT

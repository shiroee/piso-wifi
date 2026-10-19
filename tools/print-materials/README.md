# Print materials

Regenerates the customer instructions from the real portal page:

- `docs/images/print/steps-page-1.png`, `steps-page-2.png`: two A4 pages, three steps each.
- `docs/images/print/tarpaulin.jpg`: landscape 3:2 tarpaulin (5400 × 3600 px; 3 × 2 ft
  to 6 × 4 ft).

Edit `config.py` (name, Wi-Fi name, rates), then:

```sh
pip install flask playwright pillow
python -m playwright install chromium
python make.py
```

It serves the portal with sample data, screenshots it, and lays out the pages, so the
pictures always match the current portal design and your rates.

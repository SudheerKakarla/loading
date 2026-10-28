# Site file shape

This folder shows the columns the tools read. It is not a warehouse measurement and it is not the Thai site. The aisles are the two-aisle picture. The crew kilogram ratings are the same guesses as `inputs/crew.csv`. `mode_profile.csv` and `lead_times.csv` are the illustrative examples. `staging.csv` is a placeholder count. Replace every file when a site extract arrives. Put that extract in `inputs/site_th/`. Do not commit confidential rows, and do not fill `site_th` with guessed coordinates.

`python source/validate_site.py --site inputs/site_template --out outputs/site_template/travel_times.csv` builds the travel matrix at speed 1 distance-unit per minute and checks that every location in `loads.csv` is in it. `--speed` is the measured rate when one exists. `location_type` is `bulk` or `pickface`. The travel builder ignores that column.

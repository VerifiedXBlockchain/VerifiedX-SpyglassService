from django.core.management.base import BaseCommand
from django.db import connection

from rbx.models import Block
from rbx.tasks import sync_block

"""
python manage.py backfill_missing_blocks [--heights 1,2,3] [--dry-run]

Finds height gaps in the local block table and re-syncs each missing
block from the node. Gaps exist because sync_block returns silently
when the node serves unparseable/null data and the sync cursor
(max height + 1) never looks back.

Blocks are processed in ascending order so cross-transaction lookups
(e.g. withdrawal completes referencing request hashes) resolve. A
height reported as "still missing" means the node did not return the
block — check that the node still serves it.
"""


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument(
            "--heights",
            help="Comma-separated heights to sync (skips gap detection)",
        )
        parser.add_argument("--dry-run", action="store_true")

    def find_gaps(self):
        # Index-driven self-join; avoids generate_series over the full
        # 0..max range. Returns the first height of each gap plus length.
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT b.height + 1 AS gap_start,
                       (SELECT min(height) FROM rbx_block n WHERE n.height > b.height) - b.height - 1 AS gap_len
                FROM rbx_block b
                WHERE NOT EXISTS (
                    SELECT 1 FROM rbx_block n WHERE n.height = b.height + 1
                )
                AND b.height < (SELECT max(height) FROM rbx_block)
                ORDER BY gap_start
                """
            )
            gaps = cursor.fetchall()

        heights = []
        for gap_start, gap_len in gaps:
            heights.extend(range(gap_start, gap_start + gap_len))
        return heights

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        if options["heights"]:
            heights = sorted(int(h) for h in options["heights"].split(","))
        else:
            self.stdout.write("Scanning for height gaps...")
            heights = self.find_gaps()

        if not heights:
            self.stdout.write("No missing blocks found.")
            return

        self.stdout.write(f"Missing heights ({len(heights)}): {heights}")
        if dry_run:
            return

        synced, failed = 0, []
        for height in heights:
            sync_block(height)
            if Block.objects.filter(height=height).exists():
                self.stdout.write(f"{height}: synced")
                synced += 1
            else:
                # sync_block returns silently when the node has no data.
                self.stderr.write(f"{height}: STILL MISSING (node returned no data)")
                failed.append(height)

        self.stdout.write(f"Done. {synced} synced, {len(failed)} still missing.")
        if failed:
            self.stderr.write(f"Still missing: {failed}")

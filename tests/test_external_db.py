"""external_db 纯函数单测（无网络）。"""
from __future__ import annotations

import json

from external_db import clinvar_record_to_gnomad_variant_id, nc_accession_to_chrom


def test_nc_accession_to_chrom() -> None:
    assert nc_accession_to_chrom("NC_000001.11") == "1"
    assert nc_accession_to_chrom("NC_000017.11") == "17"
    assert nc_accession_to_chrom("NC_000023.11") == "X"
    assert nc_accession_to_chrom("NC_000024.10") == "Y"
    assert nc_accession_to_chrom("NC_012920.1") is None


def test_clinvar_spdi_to_gnomad_id() -> None:
    rec = json.loads(
        """
        {
          "variation_set": [{
            "canonical_spdi": "NC_000001.11:226065668:GC:AT",
            "variation_loc": []
          }]
        }
        """
    )
    assert clinvar_record_to_gnomad_variant_id(rec) == "1-226065668-GC-AT"


def test_clinvar_grch38_loc() -> None:
    rec = {
        "variation_set": [
            {
                "variation_loc": [
                    {
                        "assembly_name": "GRCh38",
                        "chr": "17",
                        "display_start": "7661779",
                        "ref": "C",
                        "alt": "T",
                    }
                ]
            }
        ]
    }
    assert clinvar_record_to_gnomad_variant_id(rec) == "17-7661779-C-T"

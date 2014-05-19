import os
import subprocess
import copy
import fastaq
from iva import mapping, mummer

class Error (Exception): pass



class Qc:
    def __init__(self, ref_fasta, ref_gff, assembly_fasta, output_prefix, reads_fwd=None, reads_rev=None, assembly_bam=None, ref_bam=None, min_ref_cov=5, contig_layout_plot_title="IVA QC contig layout and read depth", threads=1):
        for filename in ref_fasta, ref_gff, assembly_fasta:
            if not os.path.exists(filename):
                raise Error('Error in IVA QC. File not found: "' + filename + '"')
      
        self.outprefix = output_prefix
        self.assembly_bam = output_prefix + '.reads_mapped_to_assembly.bam'
        self.ref_bam = output_prefix + '.reads_mapped_to_ref.bam'

        if None in [assembly_bam, ref_bam]:
            if None in [reads_fwd, reads_rev]:
                raise Error('Must give both reads_fwd and reads_rev, or both assembly_bam and ref_bam, to Qc.__init__()')
        elif None in [reads_fwd, reads_rev]:
            if None in [assembly_bam, ref_bam]:
                raise Error('Must give both reads_fwd and reads_rev to Qc.__init__()')
            
            for bam in [assembly_bam, ref_bam]:
                if not os.path.exists(bam + '.bai'):
                    raise Error('Must index input BAM files, Did not find .bai file for bam "' + bam + '". Cannot continue')
               
            os.symlink(assembly_bam, self.assembly_bam)
            os.symlink(ref_bam, self.ref_bam)
            os.symlink(assembly_bam + '.bai', self.assembly_bam + '.bai')
            os.symlink(ref_bam + '.bai', self.ref_bam + '.bai')
            
        self.ref_gff = ref_gff
        self.min_ref_cov = min_ref_cov
        self._set_ref_fasta_data(ref_fasta)
        self._set_assembly_fasta_data(assembly_fasta)
        self.reads_fwd = reads_fwd
        self.reads_rev = reads_rev
        self.threads = threads
        self.contig_layout_plot_title = contig_layout_plot_title
        self.contig_pos_in_ref = {}
        self.low_cov_ref_regions = {}
        self.low_cov_ref_regions_fwd = {}
        self.low_cov_ref_regions_rev = {}
        self.ok_cov_ref_regions = {}
        self.ref_coverage_fwd = {}
        self.ref_coverage_rev = {}
        self.ref_cds_fasta = output_prefix + '.ref_cds_seqs.fa'
        self.cds_nucmer_coords_in_assembly = output_prefix + '.ref_cds_seqs_mapped_to_assembly.coords'
        self.cds_assembly_stats = {}
        self.refseq_assembly_stats = {}
        self.assembly_vs_ref_coords = output_prefix + '.assembly_vs_ref.coords'
        self.assembly_vs_ref_mummer_hits = {}
        self.ref_pos_covered_by_contigs = {}
        self.ref_pos_not_covered_by_contigs = {}
        self.should_have_assembled = {}
        self.contig_placement = {}
        self.stats_keys = [
            'ref_bases',
            'ref_sequences',
            'ref_bases_assembled',
            'ref_sequences_assembled',
            'ref_sequences_assembled_ok',
            'ref_bases_assembler_missed',
            'assembly_bases',
            'assembly_contigs',
            'assembly_bases_in_ref',
            'cds_number',
            'cds_assembled',
            'cds_assembled_ok',
        ]
        self.stats = {shrubbery: -1 for shrubbery in self.stats_keys}
        self.stats_file_txt = output_prefix + '.stats.txt'
        self.stats_file_tsv = output_prefix + '.stats.tsv'


    def _set_assembly_fasta_data(self, fasta_filename):
        self.assembly_fasta = fasta_filename
        self.assembly_fasta_fai = self.assembly_fasta + '.fai'
        if not os.path.exists(self.assembly_fasta_fai):
            subprocess.check_output('samtools faidx ' + self.assembly_fasta_fai, shell=True)
        self.assembly_lengths = {}
        fastaq.tasks.lengths_from_fai(self.assembly_fasta_fai, self.assembly_lengths)
        

    def _set_ref_fasta_data(self, fasta_filename):
        self.ref_fasta = fasta_filename
        self.ref_fasta_fai = self.ref_fasta + '.fai'
        if not os.path.exists(self.ref_fasta_fai):
            subprocess.check_output('samtools faidx ' + self.ref_fasta_fai, shell=True)
        self.ref_ids = self._ids_in_order_from_fai(self.ref_fasta_fai)
        self.ref_lengths = {}
        fastaq.tasks.lengths_from_fai(self.ref_fasta_fai, self.ref_lengths)

        self.ref_length_offsets = {}
        offset = 0
        for seq in self.ref_ids:
            self.ref_length_offsets[seq] = offset
            offset += self.ref_lengths[seq]
        

    def _ids_in_order_from_fai(self, filename):
        ids = []
        f = fastaq.utils.open_file_read(filename)
        for line in f:
            ids.append(line.rstrip().split('\t')[0])
        fastaq.utils.close(f)
        return ids


    def _get_ref_cds_from_gff(self):
        f = fastaq.utils.open_file_read(self.ref_gff)
        coords = {}
        for line in f:
            # no annotation allowed after any fasta sequence. See
            # http://www.sequenceontology.org/gff3.shtml
            if line.rstrip() == '##FASTA':
                break
            elif line.startswith('#'):
                continue

            data = line.rstrip().split('\t')
            if data[2] == 'CDS':
                seqname = data[0]
                start = int(data[3]) - 1
                end = int(data[4]) - 1
                strand = data[6]
                if seqname not in coords:
                    coords[seqname] = []
                coords[seqname].append((fastaq.intervals.Interval(start, end), strand))

        fastaq.utils.close(f)
        for seqname in coords:
            coords[seqname].sort()

        return coords
       

    def _write_cds_seqs(self, cds_list, fa, f_out):
        for coords, strand in cds_list:
            seqname = fa.id + ':' + str(coords.start + 1) + '-' + str(coords.end + 1) + ':' + strand
            seq = fastaq.sequences.Fasta(seqname, fa.seq[coords.start:coords.end+1])
            if strand == '-':
                seq.revcomp()
            print(seq, file=f_out)
            assert seqname not in self.cds_assembly_stats
            self.cds_assembly_stats[seqname] = {
                'ref_name': fa.id,
                'ref_coords': coords,
                'strand': strand,
                'length_in_ref': len(coords),
                'assembled': False,
                'assembled_ok': False,
            }
            
        

    def _gff_and_fasta_to_cds(self):
        cds_coords = self._get_ref_cds_from_gff()
        f = fastaq.utils.open_file_write(self.ref_cds_fasta)
        seq_reader = fastaq.sequences.file_reader(self.ref_fasta)
        for seq in seq_reader:
            if seq.id in cds_coords:
                self._write_cds_seqs(cds_coords[seq.id], seq, f)
        fastaq.utils.close(f)


    def _map_cds_to_assembly(self):
        if not os.path.exists(self.ref_cds_fasta):
            self._gff_and_fasta_to_cds()
        mummer.run_nucmer(self.ref_cds_fasta, self.assembly_fasta, self.cds_nucmer_coords_in_assembly, min_length=30, min_id=80)


    def _mummer_coords_file_to_dict(self, filename):
        hits = {}
        for hit in mummer.file_reader(filename):
            if hit.qry_name not in hits:
                hits[hit.qry_name] = []
            hits[hit.qry_name].append(copy.copy(hit))
        return hits


    def _has_orf(self, fa, start, end, min_length):
        subseq = fastaq.sequences.Fasta('seq', fa[start:end+1])
        orfs = subseq.all_orfs(min_length=min_length)
        return len(orfs) > 0


    def _calculate_cds_assembly_stats(self):
        self._map_cds_to_assembly()
        hits = self._mummer_coords_file_to_dict(self.cds_nucmer_coords_in_assembly)
        contigs = {}
        fastaq.tasks.file_to_dict(self.assembly_fasta, contigs)
        for cds_name, hit_list in hits.items():
            self.cds_assembly_stats[cds_name]['number_of_contig_hits'] = len(hit_list)
            hit_coords = [x.qry_coords() for x in hit_list]
            fastaq.intervals.merge_overlapping_in_list(hit_coords)
            bases_assembled = fastaq.intervals.length_sum_from_list(hit_coords)
            self.cds_assembly_stats[cds_name]['bases_assembled'] = bases_assembled
            self.cds_assembly_stats[cds_name]['assembled'] = 0.9 <= bases_assembled / self.cds_assembly_stats[cds_name]['length_in_ref'] <= 1.1
            
            if len(hit_list) == 1:
                hit = hit_list[0]
                contig_coords = hit.ref_coords()
                has_orf = self._has_orf(contigs[hit.ref_name], contig_coords.start, contig_coords.end, 0.9 * self.cds_assembly_stats[cds_name]['length_in_ref'])
                self.cds_assembly_stats[cds_name]['assembled_ok'] = has_orf
            else:
                self.cds_assembly_stats[cds_name]['assembled_ok'] = False


    def _get_contig_hits_to_reference(self):
        mummer.run_nucmer(self.assembly_fasta, self.ref_fasta, self.assembly_vs_ref_coords, min_id=95, min_length=100)
        self.assembly_vs_ref_mummer_hits = self._mummer_coords_file_to_dict(self.assembly_vs_ref_coords)


    def _hash_nucmer_hits_by_ref(self, hits):
        d = {}
        for l in hits.values():
            for hit in l:
                if hit.ref_name not in d:
                    d[hit.ref_name] = []
                d[hit.ref_name].append(copy.copy(hit))
        return d


    def _calculate_refseq_assembly_stats(self):
        refhits = self._hash_nucmer_hits_by_ref(self.assembly_vs_ref_mummer_hits)

        for name in self.ref_ids:
            assert name not in self.refseq_assembly_stats
            if name in refhits:
                hits = refhits[name]
                coords = [hit.ref_coords() for hit in hits]
                fastaq.intervals.merge_overlapping_in_list(coords)
                self.refseq_assembly_stats[name] = {
                    'hits': len(hits),
                    'bases_assembled': fastaq.intervals.length_sum_from_list(coords),
                    'assembled': 0.9 <= fastaq.intervals.length_sum_from_list(coords) / self.ref_lengths[name],
                    'assembled_ok': len(hits) == 1 and 0.9 <= hits[0].hit_length_ref / self.ref_lengths[name] <= 1.1
                }
            else:
                self.refseq_assembly_stats[name] = {
                    'hits': 0,
                    'bases_assembled': 0,
                    'assembled': False,
                    'assembled_ok': False,
                }

             

    def _invert_list(self, coords, seq_length):
        if len(coords) == 0:
            return[fastaq.intervals.Interval(0, seq_length - 1)]

        not_covered = []

        if coords[0].start != 0:
            not_covered.append(fastaq.intervals.Interval(0, coords[0].start - 1))

        for i in range(len(coords) - 1):
            not_covered.append(fastaq.intervals.Interval(coords[i].end + 1, coords[i+1].start - 1))

        if coords[-1].end < seq_length - 1:
            not_covered.append(fastaq.intervals.Interval(coords[-1].end + 1, seq_length - 1))
 
        return not_covered


    def _calculate_ref_positions_covered_by_contigs(self):
        for seq in self.assembly_vs_ref_mummer_hits:
            for hit in self.assembly_vs_ref_mummer_hits[seq]:
                if hit.ref_name not in self.ref_pos_covered_by_contigs:
                    self.ref_pos_covered_by_contigs[hit.ref_name] = []
                self.ref_pos_covered_by_contigs[hit.ref_name].append(hit.ref_coords())
            
        for coords_list in self.ref_pos_covered_by_contigs.values():
            fastaq.intervals.merge_overlapping_in_list(coords_list)

        for seq in self.ref_ids:
            if seq in self.ref_pos_covered_by_contigs:
                l = self.ref_pos_covered_by_contigs[seq]
            else:
                l = []
            self.ref_pos_not_covered_by_contigs[seq] = self._invert_list(l, self.ref_lengths[seq])


    def _get_overlapping_qry_hits(self, hits, hit):
        overlapping = []
        hit_coords = hit.qry_coords()
        
        for test_hit in hits:
            if test_hit != hit:
                test_coords = test_hit.qry_coords()
                if test_coords.intersects(hit_coords):
                    overlapping.append(test_hit)

        return overlapping


    def _get_unique_and_repetitive_from_contig_hits(self, hits):
        unique = []
        repetitive = []
        if len(hits) == 0:
            return unique, repetitive

        for hit in hits:
            if len(self._get_overlapping_qry_hits(hits, hit)):
                repetitive.append(hit)
            else:
                unique.append(hit)

        return unique, repetitive
             

    def _get_longest_hit_index(self, hits):
        if len(hits) == 0:
            return None
        max_length = -1
        index = -1
        for i in range(len(hits)):
            length = max(hits[i].qry_start, hits[i].qry_end) - min(hits[i].qry_start, hits[i].qry_end)
            if length > max_length:
                index = i
                max_length = length

        assert max_length != -1
        assert index != -1
        return index


    def _contig_placement_in_reference(self, hits):
        unique_hits, repetitive_hits = self._get_unique_and_repetitive_from_contig_hits(hits)
        placement = [(x.qry_coords(), x.ref_name, x.ref_coords(), x.on_same_strand(), False) for x in unique_hits]
        placement += [(x.qry_coords(), x.ref_name, x.ref_coords(), x.on_same_strand(), True) for x in repetitive_hits]
        placement.sort()
        return placement
         

    def _calculate_contig_placement(self):
        self._get_contig_hits_to_reference()
        self.contig_placement = {qry_name: self._contig_placement_in_reference(self.assembly_vs_ref_mummer_hits[qry_name]) for qry_name in self.assembly_vs_ref_mummer_hits}


    def _get_R_plot_contig_order_from_contig_placement(self):
        contig_positions = []
        for qryname, coords_list in  self.contig_placement.items():
            for qry_coords, refname, ref_coords, same_strand, repetitive in coords_list:
                offset = self.ref_length_offsets[refname]
                ref_coords = fastaq.intervals.Interval(ref_coords.start + offset, ref_coords.end + offset)
                contig_positions.append((ref_coords, qry_coords, same_strand, repetitive, qryname))

        contig_positions.sort()
        names = set()
        contig_names = []
        for name in [x[4] for x in contig_positions]:
            if name not in names:
                contig_names.append(name)
                names.add(name)
        return contig_names


    def _map_reads_to_assembly(self):
        mapping.map_reads(self.reads_fwd, self.reads_rev, self.assembly_fasta, self.assembly_bam[:-4], sort=True, threads=self.threads)


    def _calculate_ref_read_coverage(self):
        if None not in [self.reads_fwd, self.reads_rev]:
            mapping.map_reads(self.reads_fwd, self.reads_rev, self.ref_fasta, self.ref_bam[:-4], sort=True, threads=self.threads)
            os.unlink(self.ref_bam[:-4] + '.unsorted.bam')

        for seq in self.ref_ids:
            assert seq not in self.ref_coverage_fwd
            self.ref_coverage_fwd[seq] = mapping.get_bam_region_coverage(self.ref_bam, seq, self.ref_lengths[seq])
            assert seq not in self.ref_coverage_rev
            self.ref_coverage_rev[seq] = mapping.get_bam_region_coverage(self.ref_bam, seq, self.ref_lengths[seq], rev=True)



    def _coverage_list_to_low_cov_intervals(self, l):
        bad_intervals = []
        start = None
        cov_bad = False

        for i in range(len(l)):
            cov_bad = l[i] < self.min_ref_cov
            if cov_bad:
                if start is None:
                    start = i
            else:
                if start is not None:
                    bad_intervals.append(fastaq.intervals.Interval(start, i-1))
                start = None

        if cov_bad and start is not None:
            bad_intervals.append(fastaq.intervals.Interval(start, i))
        return bad_intervals
         

    def _calculate_ref_read_region_coverage(self):
        assert len(self.ref_coverage_fwd)
        assert len(self.ref_coverage_rev)
        for seq in self.ref_ids:
            self.low_cov_ref_regions_fwd[seq] = self._coverage_list_to_low_cov_intervals(self.ref_coverage_fwd[seq])
            self.low_cov_ref_regions_rev[seq] = self._coverage_list_to_low_cov_intervals(self.ref_coverage_rev[seq])
            fwd_ok = self._invert_list(self.low_cov_ref_regions_fwd[seq], self.ref_lengths[seq])
            rev_ok = self._invert_list(self.low_cov_ref_regions_rev[seq], self.ref_lengths[seq])
            self.ok_cov_ref_regions[seq] = fastaq.intervals.intersection(fwd_ok, rev_ok)
            self.low_cov_ref_regions[seq] = fastaq.intervals.intersection(self.low_cov_ref_regions_fwd[seq], self.low_cov_ref_regions_rev[seq])


    def _write_ref_coverage_to_files_for_R(self, outprefix):
        assert len(self.ref_coverage_fwd)
        assert len(self.ref_coverage_rev)
        def list_to_file(d, fname):
            f = fastaq.utils.open_file_write(fname)
            for refname in self.ref_ids:
                for x in d[refname]:
                    print(x, file=f)
            fastaq.utils.close(f)
        list_to_file(self.ref_coverage_fwd, outprefix + '.fwd')
        list_to_file(self.ref_coverage_rev, outprefix + '.rev')


    def _cov_to_R_string(self, intervals, colour, x_offset, y_position, contig_height):
        s = ''
        for interval in intervals:
            s += 'rect(' + \
                 str(interval.start + x_offset) + ', ' + \
                 str(y_position - 0.5 * contig_height) + ', ' + \
                 str(interval.end + x_offset) + ', ' + \
                 str(y_position + 0.5 * contig_height) + ', ' + \
                 'col="' + colour + '", ' + \
                 'border=NA)\n'
        return s


    def _calculate_should_have_assembled(self):
        for name in self.ref_ids:
            if name in self.ref_pos_covered_by_contigs:
                l = self.ref_pos_covered_by_contigs[name]
            else:
                l = []
            self.should_have_assembled[name] = fastaq.intervals.intersection(self._invert_list(l, self.ref_lengths[name]), self.ok_cov_ref_regions[name])


    def _do_calculations(self):
        if None not in [self.reads_fwd, self.reads_rev]:
            self._map_reads_to_assembly()
        self._calculate_contig_placement()
        self._calculate_ref_read_coverage()
        self._calculate_ref_read_region_coverage()
        self._calculate_ref_positions_covered_by_contigs()
        self._calculate_should_have_assembled()
        self._calculate_cds_assembly_stats()
        self._calculate_refseq_assembly_stats()
 


    def _contig_bases_that_hit_ref(self):
        total_bases = 0
        for name in self.assembly_vs_ref_mummer_hits:
            coords = [x.qry_coords() for x in self.assembly_vs_ref_mummer_hits[name]]
            fastaq.intervals.merge_overlapping_in_list(coords)
            total_bases += fastaq.intervals.length_sum_from_list(coords)
        return total_bases
          

    def _calculate_stats(self):
        self.stats['ref_bases'] = sum(self.ref_lengths.values())
        self.stats['ref_sequences'] = len(self.ref_lengths)
        self.stats['ref_bases_assembled'] = 0
        self.stats['ref_bases_assembled'] = sum([fastaq.intervals.length_sum_from_list(l) for l in list(self.ref_pos_covered_by_contigs.values())])
        self.stats['ref_sequences_assembled'] = len([1 for x in self.refseq_assembly_stats.values() if x['assembled']])
        self.stats['ref_sequences_assembled_ok'] = len([1 for x in self.refseq_assembly_stats.values() if x['assembled_ok']])
        self.stats['ref_bases_assembler_missed'] = sum([fastaq.intervals.length_sum_from_list(l) for l in list(self.should_have_assembled.values())])
        self.stats['assembly_bases'] = sum(self.assembly_lengths.values())
        self.stats['assembly_contigs'] = len(self.assembly_lengths)
        self.stats['assembly_bases_in_ref'] = self._contig_bases_that_hit_ref()
        self.stats['cds_number'] = len(self.cds_assembly_stats)
        self.stats['cds_assembled'] = len([1 for x in self.cds_assembly_stats.values() if x['assembled']])
        self.stats['cds_assembled_ok'] = len([1 for x in self.cds_assembly_stats.values() if x['assembled_ok']])
        

    def _write_stats_txt(self):
        f = fastaq.utils.open_file_write(self.stats_file_txt)
        for stat in self.stats_keys:
            print(stat, self.stats[stat], sep='\t', file=f)
        fastaq.utils.close(f)


    def _write_stats_tsv(self):
        f = fastaq.utils.open_file_write(self.stats_file_tsv)
        print('\t'.join([x for x in self.stats_keys]), file=f)
        print('\t'.join([str(self.stats[x]) for x in self.stats_keys]), file=f)
        fastaq.utils.close(f)


    def _write_stats_files(self):
        self._write_stats_txt()
        self._write_stats_tsv()


    def _make_R_plots(self):
        outprefix = self.outprefix + '.contig_placement'
        contig_names = self._get_R_plot_contig_order_from_contig_placement()
        number_of_contigs = len(contig_names)
        ref_length = sum(self.ref_lengths.values())
        r_script = outprefix + '.R'
        f = fastaq.utils.open_file_write(r_script)
        contig_height = 0.8
        vertical_lines = ''
        if len(self.ref_ids) > 0:
            for name in self.ref_ids:
                x_position = self.ref_length_offsets[name]
                if x_position > 0:
                    vertical_lines += '\n' + 'abline(v=' + str(x_position) + ', col="gray")'


        print('pdf(file="', outprefix, '.pdf")', sep='', file=f)
        print('layout(matrix(c(1,2), 2, 1, byrow = TRUE), heights=c(2,1))', file=f)

        # ---------- contig layout plot ------------------------
        print('plot(-100, type="n", xlim=c(0,', ref_length, '), ylim=c(0, ', number_of_contigs + 2, '), yaxt="n", ylab="", xlab="")', sep='', file=f)
        print('title("', self.contig_layout_plot_title, '", ylab="Contigs")', sep='', file=f)
        for name in self.ref_ids:
            offset = self.ref_length_offsets[name]
            print(self._cov_to_R_string(self.ok_cov_ref_regions[name], 'black', offset, 1.3, 0.25), file=f)
            print(self._cov_to_R_string(self.low_cov_ref_regions_fwd[name], 'red', offset, 1, 0.25), file=f)
            print(self._cov_to_R_string(self.low_cov_ref_regions_rev[name], 'red', offset, 0.7, 0.25), file=f)

            if name in self.ref_pos_covered_by_contigs:
                print(self._cov_to_R_string(self.ref_pos_covered_by_contigs[name], 'black', offset, 2.3, 0.25), file=f)

            if name in self.should_have_assembled:
                print(self._cov_to_R_string(self.should_have_assembled[name], 'red', offset, 1.7, 0.25), file=f)

            print(self._cov_to_R_string(self.ref_pos_not_covered_by_contigs[name], 'black', offset, 2, 0.25), file=f)

        print(vertical_lines, file=f)
       
        if number_of_contigs > 0:
            print('contig_names=c("ref read cov", "ref contig cov", "', '", "'.join(contig_names), '")', sep='', file=f)
            print('axis(2, at=c(1:', number_of_contigs + 2, '), labels=contig_names, las=2, cex.axis=0.3)', sep='', file=f)

            for i in range(len(contig_names)):
                contig_name = contig_names[i]
                y_centre = i + 3
                contig_positions = self.contig_placement[contig_name]
                for contig_coords, ref_name, ref_coords, same_strand, repetitive in contig_positions:
                    offset = self.ref_length_offsets[ref_name]
                    if repetitive:
                        colour = "red"
                    else:
                        colour = "blue"
 
                    if same_strand:
                         colour = "dark" + colour

                    print('rect(', ref_coords.start + offset, ',',
                            y_centre - 0.5 * contig_height, ',',
                            ref_coords.end + offset, ',',
                            y_centre + 0.5 * contig_height, ',',
                            'col="', colour, '")', sep='', file=f)
                
        # ----------- read depth on reference plot --------------
        self._write_ref_coverage_to_files_for_R(self.outprefix + '.read_coverage_on_ref')
        print('fwd_ref_cov = scan("', self.outprefix + '.read_coverage_on_ref.fwd', '")', sep='', file=f)
        print('rev_ref_cov = scan("', self.outprefix + '.read_coverage_on_ref.rev', '")', sep='', file=f)
        print('plot(fwd_ref_cov, type="l", xlim=c(0, length(fwd_ref_cov) + 1), ylim=c(-max(rev_ref_cov), max(fwd_ref_cov)), col="blue", frame.plot=F, ylab="Read depth", xlab="Position in reference")', file=f)
        print('lines(-rev_ref_cov, col="blue")', file=f)
        print('abline(h=0, lty=2)', file=f)
        print(vertical_lines, file=f)

        print('dev.off()', file=f)
        fastaq.utils.close(f)


    def run(self):
        self._do_calculations()
        self._make_R_plots()
        self._calculate_stats()
        self._write_stats_files()
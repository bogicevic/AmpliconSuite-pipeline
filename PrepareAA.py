#!/usr/bin/env python

# author: Jens Luebeck (jluebeck [at] ucsd.edu)

import argparse
from datetime import datetime
import gzip
import json
import os
import socket
from subprocess import *
import sys
import threading
import time

import check_reference
import cnv_prefilter

__version__ = "0.1203.13"

PY3_PATH = "python3"  # updated by command-line arg if specified
metadata_dict = {}
sample_info_dict = {}

# generic worker thread function
class workerThread(threading.Thread):
    def __init__(self, threadID, target, *args):
        threading.Thread.__init__(self)
        self.threadID = threadID
        self._target = target
        self._args = args
        threading.Thread.__init__(self)

    def run(self):
        self._target(*self._args)


def run_bwa(ref, fastqs, outdir, sname, nthreads, usingDeprecatedSamtools=False):
    outname = outdir + sname
    print(outname)
    print("Checking for ref index")
    exts = [".sa", ".amb", ".ann", ".pac", ".bwt"]
    indexPresent = True
    for i in exts:
        if not os.path.exists(ref + i):
            indexPresent = False
            print("Could not find " + ref + i + ", building BWA index from scratch. This could take > 60 minutes")
            break

    if not indexPresent:
        cmd = "bwa index " + ref
        call(cmd, shell=True)

    print("\nPerforming alignment and sorting")
    if usingDeprecatedSamtools:
        cmd = "{{ bwa mem -K 10000000 -t {} {} {} | samtools view -Shu - | samtools sort -m 4G -@4 - {}.cs; }} 2>{}_aln_stage.stderr".format(
            nthreads, ref, fastqs, outname, outname)
    else:
        cmd = "{{ bwa mem -K 10000000 -t {} {} {} | samtools view -Shu - | samtools sort -m 4G -@4 -o {}.cs.bam -; }} 2>{}_aln_stage.stderr".format(
            nthreads, ref, fastqs, outname, outname)

    print(cmd)
    call(cmd, shell=True)
    metadata_dict["bwa_cmd"] = cmd
    print("\nPerforming duplicate removal & indexing")
    cmd_list = ["samtools", "rmdup", "-s", "{}.cs.bam".format(outname), "{}.cs.rmdup.bam".format(outname)]
    print(" ".join(cmd_list))
    call(cmd_list)
    print("\nRunning samtools index")
    cmd_list = ["samtools", "index", "{}.cs.rmdup.bam".format(outname)]
    print(" ".join(cmd_list))
    call(cmd_list)
    print("Removing temp BAM")
    cmd = "rm {}.cs.bam".format(outname)
    call(cmd, shell=True)
    return outname + ".cs.rmdup.bam"


def run_freebayes(ref, bam_file, outdir, sname, nthreads, regions, fb_path=None):
    # Freebayes cmd-line args
    # -f is fasta
    # -r is region to call
    fb_exec = "freebayes"
    if fb_path:
        fb_exec = fb_path + "/" + fb_exec
    while True:
        try:
            curr_region_tup = regions.pop()
        except IndexError:
            break

        curr_region_string = curr_region_tup[0] + ":" + curr_region_tup[1]
        print(curr_region_string + ". " + str(len(regions)) + " items remaining.")
        vcf_file = outdir + sname + "_" + curr_region_tup[0] + "_" + curr_region_tup[2] + ".vcf"
        replace_filter_field_func = "awk '{ if (substr($1,1,1) != \"#\" ) { $7 = ($7 == \".\" ? \"PASS\" : $7 ) }} 1 ' OFS=\"\\t\""
        cmd = "{} --genotype-qualities --standard-filters --use-best-n-alleles 5 --limit-coverage 25000 \
        --strict-vcf -f {} -r {} {} | {} > {}".format(fb_exec, ref, curr_region_string, bam_file,
                                                      replace_filter_field_func, vcf_file)
        call(cmd, shell=True)
        # gzip the new VCF
        call("gzip -f " + vcf_file, shell=True)


def run_canvas(canvas_dir, bam_file, vcf_file, outdir, removed_regions_bed, sname, ref):
    # Canvas cmd-line args
    # -b: bam
    # --sample-b-allele-vcf: vcf
    # -n: sample name
    # -o: output directory
    # -r: reference fasta
    # -g: "folder with genome.fa and genomesize xml
    # -f: regions to ignore

    print("\nCalling Canvas")
    ref_repo = canvas_dir + "/canvasdata/" + args.ref + "/"
    # cmd = "{}/Canvas Germline-WGS -b {} --sample-b-allele-vcf={} --ploidy-vcf={}\
    # -n {} -o {} -r {} -g {} -f {} > {}/canvas_stdout.log".format(canvas_dir,bam_file, \
    # vcf_file, ploidy_vcf, sname, outdir, ref, ref_repo, removed_regions_bed, outdir)
    cmd = "{}/Canvas Germline-WGS -b {} --sample-b-allele-vcf={} --ploidy-vcf={} -n {} -o {} -r {} -g {} -f {} > {}/canvas_stdout.log".format(
        canvas_dir, bam_file, vcf_file, ploidy_vcf, sname, outdir, ref, ref_repo, removed_regions_bed, outdir)

    print(cmd)
    call(cmd, shell=True, executable="/bin/bash")


def run_cnvkit(ckpy_path, nthreads, outdir, bamfile, seg_meth='cbs', normal=None, refG=None, vcf=None):
    # CNVkit cmd-line args
    # -m wgs: wgs data
    # -y: assume chrY present
    # -n: create flat reference (cnv baseline)
    # -p: number of threads
    # -f: reference genome fasta
    bamBase = os.path.splitext(os.path.basename(bamfile))[0]
    if not ckpy_path.endswith("/cnvkit.py"):
        ckpy_path += "/cnvkit.py"

    cnvkit_version = Popen([PY3_PATH, ckpy_path, "version"], stdout=PIPE, stderr=PIPE).communicate()[0].rstrip()
    try:
        cnvkit_version = cnvkit_version.decode('utf-8')
    except UnicodeError:
        pass

    metadata_dict["cnvkit_version"] = cnvkit_version

    ckRef = AA_REPO + args.ref + "/" + args.ref + "_cnvkit_filtered_ref.cnn"
    print("\nRunning CNVKit batch")
    if args.normal_bam:
        cmd = "{} {} batch {} -m wgs --fasta {} -p {} -d {} --normal {}".format(PY3_PATH, ckpy_path, bamfile, refG, nthreads,
                                                                                        outdir, normal)
    else:
        cmd = "{} {} batch -m wgs -r {} -p {} -d {} {}".format(PY3_PATH, ckpy_path, ckRef, nthreads, outdir, bamfile)

    print(cmd)
    call(cmd, shell=True)
    metadata_dict["cnvkit_cmd"] = cmd + " ; "
    rscript_str = ""
    if args.rscript_path:
        if not args.rscript_path.endswith("/Rscript"):
            args.rscript_path += "/Rscript"

        rscript_str = "--rscript-path " + args.rscript_path
        print("Set Rscript flag: " + rscript_str)

    cnrFile = outdir + bamBase + ".cnr"
    cnsFile = outdir + bamBase + ".cns"
    print("\nRunning CNVKit segment")
    # TODO: possibly include support for adding VCF calls.
    cmd = "{} {} segment {} {} -p {} -m {} -o {}".format(PY3_PATH, ckpy_path, cnrFile, rscript_str, nthreads, seg_meth,
                                                         cnsFile)
    print(cmd)
    call(cmd, shell=True)
    metadata_dict["cnvkit_cmd"] = metadata_dict["cnvkit_cmd"] + cmd
    print("\nCleaning up temporary files")
    cmd = "rm {}/*tmp.bed {}/*.cnn {}/*target.bed".format(outdir, outdir, outdir)
    print(cmd)
    call(cmd, shell=True)
    cmd = "gzip -f " + cnrFile
    print(cmd)
    call(cmd, shell=True)


def merge_and_filter_vcfs(chr_names, vcf_list, outdir, sname):
    print("\nMerging VCFs and zipping")
    # collect the vcf files to merge
    merged_vcf_file = outdir + sname + "_merged.vcf"
    relevant_vcfs = [x for x in vcf_list if any([i in x for i in chr_names])]
    chrom_vcf_d = {}
    for f in relevant_vcfs:
        curr_chrom = f.rsplit(".vcf.gz")[0].rsplit("_")[-2:]
        chrom_vcf_d[curr_chrom[0] + curr_chrom[1]] = f

    # chr_nums = [x.lstrip("chr") for x in chr_names]
    pre_chr_str_names = [str(x) for x in range(1, 23)] + ["X", "Y"]

    # sort the elements
    # include the header from the first one
    if args.ref != "GRCh37" and args.ref != "GRCm38":
        sorted_chr_names = ["chr" + str(x) for x in pre_chr_str_names]
        cmd = "zcat " + chrom_vcf_d["chrM"] + ''' | awk '$4 != "N"' > ''' + merged_vcf_file

    else:
        sorted_chr_names = [str(x) for x in pre_chr_str_names]
        cmd = "zcat " + chrom_vcf_d["MT"] + ''' | awk '$4 != "N"' > ''' + merged_vcf_file

    print(cmd)
    call(cmd, shell=True)

    # zcat the rest, grepping out all header lines starting with "#"
    print(sorted_chr_names)
    for i in sorted_chr_names:
        if i == "chrM" or i == "MT":
            continue

        cmd_p = "zcat " + chrom_vcf_d[i + "p"] + ''' | grep -v "^#" | awk '$4 != "N"' >> ''' + merged_vcf_file
        cmd_q = "zcat " + chrom_vcf_d[i + "q"] + ''' | grep -v "^#" | awk '$4 != "N"' >> ''' + merged_vcf_file
        print(cmd_p)
        call(cmd_p, shell=True)
        print(cmd_q)
        call(cmd_q, shell=True)

    cmd = "gzip -f " + merged_vcf_file
    print(cmd)
    call(cmd, shell=True)

    return merged_vcf_file + ".gz"


def convert_canvas_cnv_to_seeds(canvas_output_directory):
    # convert the Canvas output to a BED format
    with gzip.open(canvas_output_directory + "/CNV.vcf.gz", 'rb') as infile, open(
            canvas_output_directory + "/CNV_GAIN.bed", 'w') as outfile:
        for line in infile:
            if line.startswith("#"):
                if line.startswith("#CHROM"):
                    head_fields = line[1:].rstrip().rsplit("\t")

            else:
                fields = line.rstrip().rsplit("\t")
                if "GAIN" in fields[2]:
                    chrom = fields[0]
                    start = fields[1]
                    end = fields[2].rsplit(":")[3].rsplit("-")[1]
                    chrom_num = fields[-1].rsplit(":")[3]
                    outline = "\t".join([chrom, start, end, fields[4], chrom_num]) + "\n"
                    outfile.write(outline)

    return canvas_output_directory + "/CNV_GAIN.bed"


# Read the CNVkit .cns files
def convert_cnvkit_cnv_to_seeds(cnvkit_output_directory, base, cnsfile=None, rescaled=False, nofilter=False):
    if cnsfile is None:
        if not rescaled:
            cnsfile = cnvkit_output_directory + base + ".cns"
        else:
            cnsfile = cnvkit_output_directory + base + "_rescaled.cns"

    with open(cnsfile) as infile, open(cnvkit_output_directory + base + "_CNV_CALLS.bed", 'w') as outfile:
        head = next(infile).rstrip().rsplit("\t")
        for line in infile:
            fields = line.rstrip().rsplit("\t")
            s, e = int(fields[1]), int(fields[2])
            cn_r = float(fields[4])
            cn = 2 ** (cn_r + 1)
            # if cn >= args.cngain or nofilter or rescaled:  # do not filter on size since amplified_intervals.py will merge small ones.
            outline = "\t".join(fields[0:3] + ["CNVkit", str(cn)]) + "\n"
            outfile.write(outline)

    return cnvkit_output_directory + base + "_CNV_CALLS.bed"


def rescale_cnvkit_calls(ckpy_path, cnvkit_output_directory, base, cnsfile=None, ploidy=None, purity=None):
    if not purity and not ploidy:
        print("Warning: Rescaling called without --ploidy or --purity. Rescaling will have no effect.")
    if cnsfile is None:
        cnsfile = cnvkit_output_directory + base + ".cns"

    if not ckpy_path.endswith("/cnvkit.py"):
        ckpy_path += "/cnvkit.py"

    cmd = "{} {} call {} -m clonal".format(PY3_PATH, ckpy_path, cnsfile)
    if purity:
        cmd += " --purity " + str(purity)
    if ploidy:
        cmd += " --ploidy " + str(ploidy)

    cmd += " -o " + cnvkit_output_directory + base + "_rescaled.cns"
    print("Rescaling CNVKit calls\n" + cmd)
    call(cmd, shell=True)


def run_amplified_intervals(AA_interpreter, CNV_seeds_filename, sorted_bam, output_directory, sname, cngain, cnsize_min):
    print("\nRunning amplified_intervals")
    AA_seeds_filename = "{}_AA_CNV_SEEDS".format(output_directory + sname)
    cmd = "{} {}/amplified_intervals.py --ref {} --bed {} --bam {} --gain {} --cnsize_min {} --out {}".format(
        AA_interpreter, AA_SRC, args.ref, CNV_seeds_filename, sorted_bam, str(cngain), str(cnsize_min),
        AA_seeds_filename)
    print(cmd)
    call(cmd, shell=True)
    metadata_dict["amplified_intervals_cmd"] = cmd
    return AA_seeds_filename + ".bed"


def run_AA(AA_interpreter, amplified_interval_bed, sorted_bam, AA_outdir, sname, downsample, ref, runmode, extendmode,
           insert_sdevs):
    AA_version = Popen([AA_interpreter, AA_SRC + "/AmpliconArchitect.py", "--version"], stdout=PIPE, stderr=PIPE).communicate()[1].rstrip()
    try:
        AA_version = AA_version.decode('utf-8')
    except UnicodeError:
        pass

    metadata_dict["AA_version"] = AA_version

    cmd = "{} {}/AmpliconArchitect.py --ref {} --downsample {} --bed {} --bam {} --runmode {} --extendmode {} --insert_sdevs {} --out {}/{}".format(
        AA_interpreter, AA_SRC, ref, str(downsample), amplified_interval_bed, sorted_bam, runmode, extendmode, str(insert_sdevs), AA_outdir, sname)
    print(cmd)
    call(cmd, shell=True)
    metadata_dict["AA_cmd"] = cmd


def run_AC(AA_outdir, sname, ref, AC_outdir, AC_src):
    print("\nRunning AC")
    # make input file
    class_output = AC_outdir + sname
    cmd = "{}/make_input.sh {} {}".format(AC_src, AA_outdir, class_output)
    print(cmd)
    call(cmd, shell=True)

    # run AC on input file
    input_file = class_output + ".input"

    with open(input_file) as ifile:
        sample_info_dict["number_of_AA_amplicons"] = len(ifile.readlines())

    cmd = "{} {}/amplicon_classifier.py -i {} --ref {} -o {} --report_complexity".format(PY3_PATH, AC_src, input_file,
                                                                                         ref, class_output)
    print(cmd)
    call(cmd, shell=True)
    metadata_dict["AC_cmd"] = cmd

    AC_version = Popen([PY3_PATH, AC_src + "/amplicon_classifier.py", "--version"], stdout=PIPE, stderr=PIPE).communicate()[0].rstrip()
    try:
        AC_version = AC_version.decode('utf-8')
    except UnicodeError:
        pass

    metadata_dict["AC_version"] = AC_version


def make_AC_table(sname, AC_outdir, AC_src, metadata_file, cnv_bed=None):
    # make the AC output table
    class_output = AC_outdir + sname
    input_file = class_output + ".input"
    classification_file = class_output + "_amplicon_classification_profiles.tsv"
    cmd = "{} {}/make_results_table.py -i {} --classification_file {}".format(PY3_PATH, AC_src, input_file,
                                                                              classification_file)
    if cnv_bed:
        cmd += " --cnv_bed " + cnv_bed
    if metadata_file and not metadata_file.lower() == "none":
        cmd += " --metadata_dict " + metadata_file

    print(cmd)
    call(cmd, shell=True)
    with open(class_output + "_result_table.tsv") as ifile:
        sample_info_dict["number_of_AA_features"] = len(ifile.readlines())


def get_ref_sizes(ref_genome_size_file):
    chr_sizes = {}
    with open(ref_genome_size_file) as infile:
        for line in infile:
            fields = line.rstrip().rsplit()
            if fields:
                chr_sizes[fields[0]] = str(int(fields[1]) - 1)

    return chr_sizes


def get_ref_centromeres(ref_name):
    centromere_dict = {}
    fnameD = {"GRCh38": "GRCh38_centromere.bed", "GRCh37": "human_g1k_v37_centromere.bed", "hg19": "hg19_centromere.bed",
              "mm10": "mm10_centromere.bed", "GRCm38": "GRCm38_centromere.bed", "GRCh38_viral": "GRCh38_centromere.bed"}
    with open(AA_REPO + ref_name + "/" + fnameD[ref_name]) as infile:
        for line in infile:
            if not "centromere" in line and not "acen" in line:
                continue
            fields = line.rstrip().rsplit("\t")
            if fields[0] not in centromere_dict:
                centromere_dict[fields[0]] = (fields[1], fields[2])

            else:
                pmin = min(int(centromere_dict[fields[0]][0]), int(fields[1]))
                pmax = max(int(centromere_dict[fields[0]][1]), int(fields[2]))
                # pad with 20kb
                centromere_dict[fields[0]] = (str(pmin - 20000), str(pmax + 20000))

    return centromere_dict


def save_run_metadata(outdir, sname, args, launchtime):
    # make a dictionary that stores
    # datetime
    # hostname
    # ref
    # PAA command
    # AA python interpreter version
    # bwa cmd
    # CN cmd
    # AA cmd
    # PAA version
    # CNVKit version
    # AA version
    # AC version
    metadata_dict["launch_datetime"] = launchtime
    metadata_dict["hostname"] = socket.gethostname()
    metadata_dict["ref_genome"] = args.ref
    aapint = args.aa_python_interpreter if args.aa_python_interpreter else "python"
    aa_python_v = Popen([aapint, "--version"], stdout=PIPE, stderr=PIPE).communicate()[1].rstrip()
    try:
        aa_python_v = aa_python_v.decode('utf-8')
    except UnicodeError:
        pass

    metadata_dict["AA_python_version"] = aa_python_v

    commandstring = ""
    for arg in sys.argv:
        if ' ' in arg:
            commandstring += '"{}" '.format(arg)
        else:
            commandstring += "{} ".format(arg)

    metadata_dict["PAA_command"] = commandstring
    metadata_dict["PAA_version"] = __version__

    for x in ["bwa_cmd", "cnvkit_cmd", "amplified_intervals_cmd", "AA_cmd", "AC_cmd", "cnvkit_version", "AA_version",
              "AC_version"]:
        if x not in metadata_dict:
            metadata_dict[x] = "NA"

    # save the json dict
    metadata_filename = outdir + sname + "_run_metadata.json"
    with open(metadata_filename, 'w') as fp:
        json.dump(metadata_dict, fp)

    sample_info_dict["run_metadata_file"] = metadata_filename
    return metadata_filename


# MAIN #
if __name__ == '__main__':
    # Parses the command line arguments
    parser = argparse.ArgumentParser(
        description="A simple pipeline wrapper for AmpliconArchitect, invoking alignment, variant calling, "
                    "and CNV calling prior to AA. The CNV calling is necessary for running AA")
    parser.add_argument("-o", "--output_directory", help="output directory names (will create if not already created)")
    parser.add_argument("-s", "--sample_name", help="sample name", required=True)
    parser.add_argument("-t", "--nthreads", help="Number of threads to use in BWA and CNV calling", required=True)
    parser.add_argument("--run_AA", help="Run AA after all files prepared. Default off.", action='store_true')
    parser.add_argument("--run_AC", help="Run AmpliconClassifier after all files prepared. Default off.",
                        action='store_true')
    parser.add_argument("--ref", help="Reference genome version.", choices=["hg19", "GRCh37", "GRCh38", "hg38", "mm10",
                        "GRCm38", "GRCh38_viral"])
    parser.add_argument("--cngain", type=float, help="CN gain threshold to consider for AA seeding", default=4.5)
    parser.add_argument("--cnsize_min", type=int, help="CN interval size (in bp) to consider for AA seeding",
                        default=50000)
    parser.add_argument("--downsample", type=float, help="AA downsample argument (see AA documentation)", default=10)
    parser.add_argument("--use_old_samtools", help="Indicate you are using an old build of samtools (prior to version "
                        "1.0)", action='store_true', default=False)
    parser.add_argument("--rscript_path", help="Specify custom path to Rscript, if needed when using CNVKit "
                        "(which requires R version >3.4)")
    parser.add_argument("--python3_path", help="If needed, specify a custom path to python3.")
    parser.add_argument("--aa_python_interpreter", help="By default PrepareAA will use the system's default python "
                        "path. If you would like to use a different python version with AA, set this to either the "
                        "path to the interpreter or 'python3' or 'python2'", type=str, default='python')
    parser.add_argument("--freebayes_dir", help="Path to directory where freebayes executable exists (not the path to "
                        "the executable itself). Only needed if using Canvas and freebayes is not installed on system "
                        "path.", default=None)
    parser.add_argument("--vcf", help="VCF (in Canvas format, i.e., \"PASS\" in filter field, AD field as 4th entry of "
                        "FORMAT field). When supplied with \"--sorted_bam\", pipeline will start from Canvas CNV stage."
                        )
    parser.add_argument("--aa_data_repo", help="Specify a custom $AA_DATA_REPO path FOR PRELIMINARY STEPS ONLY(!). Will"
                        " not override bash variable during AA")
    parser.add_argument("--aa_src", help="Specify a custom $AA_SRC path. Overrides the bash variable")
    parser.add_argument("--AA_runmode", help="If --run_AA selected, set the --runmode argument to AA. Default mode is "
                        "'FULL'", choices=['FULL', 'BPGRAPH', 'CYCLES', 'SVVIEW'], default='FULL')
    parser.add_argument("--AA_extendmode", help="If --run_AA selected, set the --extendmode argument to AA. Default "
                        "mode is 'EXPLORE'", choices=["EXPLORE", "CLUSTERED", "UNCLUSTERED", "VIRAL"],
                        default='EXPLORE')
    parser.add_argument("--AA_insert_sdevs", help="Number of standard deviations around the insert size. May need to "
                        "increase for sequencing runs with high variance after insert size selection step. (default "
                        "3.0)", type=float, default=3.0)
    parser.add_argument("--normal_bam", help="Path to matched normal bam for CNVKit (optional)", default=None)
    parser.add_argument("--ploidy", type=int, help="Ploidy estimate for CNVKit (optional)", default=None)
    parser.add_argument("--purity", type=float, help="Tumor purity estimate for CNVKit (optional)", default=None)
    # parser.add_argument("--no_CN_prefilter", help="Pre-filter CNV calls on number of copies gained above median "
    #                     "chromosome arm CN. Strongly recommended if input CNV calls have been scaled by purity or "
    #                     "ploidy. This argument is off by default but is set if --ploidy or --purity is provided for"
    #                     "CNVKit.", action='store_true')
    parser.add_argument("--cnvkit_segmentation", help="Segmentation method for CNVKit (if used), defaults to CNVKit "
                        "default segmentation method (cbs).", choices=['cbs', 'haar', 'hmm', 'hmm-tumor',
                        'hmm-germline', 'none'], default='cbs')
    parser.add_argument("--no_filter", help="Do not run amplified_intervals.py to identify amplified seeds",
                        action='store_true')
    parser.add_argument("--no_QC", help="Skip QC on the BAM file.", action='store_true')
    parser.add_argument("--sample_metadata", help="Path to a JSON of sample metadata to build on")
    parser.add_argument("-v", "--version", action='version',
                        version='PrepareAA version {version} \n'.format(version=__version__))
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--sorted_bam", "--bam", help="Coordinate sorted BAM file (aligned to an AA-supported "
                                                     "reference.)")
    group.add_argument("--fastqs", help="Fastq files (r1.fq r2.fq)", nargs=2)
    group.add_argument("--completed_AA_runs", help="Path to a directory containing one or more completed AA runs which utilized the same reference genome.")
    group2 = parser.add_mutually_exclusive_group(required=True)
    group2.add_argument("--reuse_canvas", help="Start using previously generated Canvas results. Identify amplified "
                        "intervals immediately.", action='store_true')
    group2.add_argument("--cnv_bed", "--bed", help="BED file (or CNVKit .cns file) of CNV changes. Fields in the bed file should"
                        " be: chr start end name cngain", default="")
    group2.add_argument("--canvas_dir", help="Path to folder with Canvas executable and \"/canvasdata\" folder "
                        "(reference files organized by reference name).", default="")
    group2.add_argument("--cnvkit_dir", help="Path to cnvkit.py", default="")
    group2.add_argument("--completed_run_metadata", help="Metadata JSON from standard runs. If you do not have it, set to 'None'.", default="")
    group2.add_argument("--align_only", help="Only perform the alignment stage (do not run CNV calling and seeding",
                        action='store_true')

    ta = time.time()
    ti = ta
    args = parser.parse_args()
    launchtime = str(datetime.now())
    print(launchtime)
    print("PrepareAA version " + __version__ + "\n")
    # set an output directory if user did not specify
    if not args.output_directory:
        args.output_directory = os.getcwd()

    if not args.output_directory.endswith("/"):
        args.output_directory += "/"

    # Make and clear necessary directories.
    # make the output directory location if it does not exist
    if not os.path.exists(args.output_directory):
        os.mkdir(args.output_directory)

    if "/" in args.sample_name:
        sys.stderr.write("Sample name -s cannot be a path. Specify output directory with -o.\n")
        sys.exit(1)

    logfile = open(args.output_directory + args.sample_name + '_timing_log.txt', 'w')
    logfile.write("#stage:\twalltime(seconds)\n")

    # Check if expected system paths and files are present. Check if provided argument combinations are valid.
    if args.aa_data_repo:
        os.environ['AA_DATA_REPO'] = args.aa_data_repo

    if args.aa_src:
        os.environ['AA_SRC'] = args.aa_src

    # Check if AA_REPO set, print error and quit if not
    try:
        AA_REPO = os.environ['AA_DATA_REPO'] + "/"

    except KeyError:
        sys.stderr.write("AA_DATA_REPO bash variable not found. AmpliconArchitect may not be properly installed.\n")
        sys.exit(1)

    if not os.path.exists(os.path.join(AA_REPO, "coverage.stats")):
        print("coverage.stats file not found in " + AA_REPO + "\nCreating a new coverage.stats file.")
        cmd = "touch {}coverage.stats && chmod a+rw {}coverage.stats".format(AA_REPO, AA_REPO)
        print(cmd)
        call(cmd, shell=True)

    try:
        AA_SRC = os.environ['AA_SRC']

    except KeyError:
        sys.stderr.write("AA_SRC bash variable not found. AmpliconArchitect may not be properly installed.\n")
        sys.exit(1)

    if (args.fastqs or args.completed_AA_runs) and not args.ref:
        sys.stderr.write("Must specify --ref when providing unaligned fastq files.\n")
        sys.exit(1)

    runCNV = None
    if args.canvas_dir:
        runCNV = "Canvas"

    elif args.cnvkit_dir:
        runCNV = "CNVkit"

    if args.python3_path:
        if not args.python3_path.endswith("/python") and not args.python3_path.endswith("/python3"):
            args.python3_path += "/python3"

        PY3_PATH = args.python3_path

    refFnames = {x: None for x in ["hg19", "GRCh37", "GRCh38", "GRCh38_viral", "mm10"]}
    # Paths of all the repo files needed
    if args.ref == "hg38":
        args.ref = "GRCh38"
    if args.ref == "GRCm38":
        args.ref = "mm10"

    for rname in refFnames.keys():
        if os.path.exists(AA_REPO + "/" + rname):
            refFnames[rname] = check_reference.get_ref_fname(AA_REPO, rname)

    faidict = {}
    if args.sorted_bam:
        if args.ref:
            faidict[args.ref] = AA_REPO + args.ref + "/" + refFnames[args.ref] + ".fai"

        else:
            for k, v in refFnames.items():
                if v:
                    faidict[k] = AA_REPO + k + "/" + v + ".fai"

        determined_ref = check_reference.check_ref(args.sorted_bam, faidict)
        if not determined_ref and not args.ref:
            sys.exit(1)

        elif not args.ref:
            args.ref = determined_ref

        elif args.ref and not determined_ref:
            print("WARNING! The BAM file did not match " + args.ref)

    gdir = AA_REPO + args.ref + "/"
    ref = gdir + refFnames[args.ref]
    ref_genome_size_file = gdir + args.ref + "_noAlt.fa.fai"
    removed_regions_bed = gdir + args.ref + "_merged_centromeres_conserved_sorted.bed"
    ploidy_vcf = gdir + "dummy_ploidy.vcf"
    merged_vcf_file = args.vcf
    if not os.path.isfile(ploidy_vcf) or not os.path.isfile(removed_regions_bed):
        sys.stderr.write(str(os.listdir(gdir)) + "\n")
        sys.stderr.write("PrepareAA data repo files not found in AA data repo. Please update your data repo.\n")
        sys.exit(1)

    # check if user gave a correct path to Canvas data repo
    if not args.cnv_bed:
        if args.canvas_dir and not os.path.exists(args.canvas_dir):
            sys.stderr.write("Could not locate Canvas data repo folder\n")
            sys.exit(1)

    canvas_output_directory = args.output_directory + "canvas_output/"
    if not os.path.exists(canvas_output_directory) and runCNV == "Canvas":
        os.mkdir(canvas_output_directory)

    # clear old results Canvas results
    elif runCNV == "Canvas":
        print("Clearing previous Canvas results")
        call("rm -rf {}/TempCNV*".format(canvas_output_directory), shell=True)
        call("rm -rf {}/Logging".format(canvas_output_directory), shell=True)
        call("rm -rf {}/Checkpoints".format(canvas_output_directory), shell=True)

    elif args.cnv_bed and not os.path.isfile(args.cnv_bed):
        sys.stderr.write("Specified CNV bed file does not exist: " + args.cnv_bed + "\n")
        sys.exit(1)

    if not args.sample_metadata:
        args.sample_metadata = os.path.dirname(os.path.realpath(__file__)) + "/sample_metadata_skeleton.json"

    with open(args.sample_metadata) as input_json:
        sample_info_dict = json.load(input_json)

    sname = args.sample_name
    sample_info_dict["sample_name"] = sname
    outdir = args.output_directory

    tb = time.time()
    logfile.write("Initialization:\t" + "{:.2f}".format(tb - ta) + "\n")
    ta = tb
    print("Running PrepareAA on sample: " + sname)
    # Begin PrepareAA pipeline
    if args.fastqs:
        # Run BWA
        fastqs = " ".join(args.fastqs)
        print("Running pipeline on " + fastqs)
        args.sorted_bam = run_bwa(ref, fastqs, outdir, sname, args.nthreads, args.use_old_samtools)

    if not args.completed_AA_runs:
        bamBaiNoExt = args.sorted_bam[:-3] + "bai"
        cramCraiNoExt = args.sorted_bam[:-4] + "crai"
        baiExists = os.path.isfile(args.sorted_bam + ".bai") or os.path.isfile(bamBaiNoExt)
        craiExists = os.path.isfile(args.sorted_bam + ".crai") or os.path.isfile(cramCraiNoExt)
        if not baiExists and not craiExists:
            print(args.sorted_bam + " index not found, calling samtools index")
            call(["samtools", "index", args.sorted_bam])
            print("Finished indexing")

        bambase = os.path.splitext(os.path.basename(args.sorted_bam))[0]
        if not args.no_QC:
            check_reference.check_properly_paired(args.sorted_bam)

        tb = time.time()
        logfile.write("Alignment and bam indexing:\t" + "{:.2f}".format(tb - ta) + "\n")

        if args.align_only:
            print("Completed\n")
            print(str(datetime.now()))
            tf = time.time()
            logfile.write("Total_elapsed_walltime\t" + "{:.2f}".format(tf - ti) + "\n")
            logfile.close()
            sys.exit()

        ta = tb
        centromere_dict = get_ref_centromeres(args.ref)
        chr_sizes = get_ref_sizes(ref_genome_size_file)
        # coordinate CNV calling
        if runCNV == "Canvas":
            # chunk the genome by chr
            regions = []
            for key, value in chr_sizes.items():
                try:
                    cent_tup = centromere_dict[key]
                    regions.append((key, "0-" + cent_tup[0], "p"))
                    regions.append((key, cent_tup[1] + "-" + value, "q"))

                # handle mitochondrial contig
                except KeyError:
                    regions.append((key, "0-" + value, ""))

            if not merged_vcf_file:
                # Run FreeBayes, one instance per chromosome
                print("\nRunning freebayes")
                print("Using freebayes version:")
                call("freebayes --version", shell=True)
                freebayes_output_directory = args.output_directory + "freebayes_vcfs/"
                if not os.path.exists(freebayes_output_directory):
                    os.mkdir(freebayes_output_directory)

                threadL = []
                for i in range(int(args.nthreads)):
                    threadL.append(workerThread(i, run_freebayes, ref, args.sorted_bam, freebayes_output_directory, sname,
                                                args.nthreads, regions, args.freebayes_dir))
                    threadL[i].start()

                for t in threadL:
                    t.join()

                # make a list of vcf files
                vcf_files = [freebayes_output_directory + x for x in os.listdir(freebayes_output_directory) if
                             x.endswith(".vcf.gz")]

                # MERGE VCFs
                merged_vcf_file = merge_and_filter_vcfs(chr_sizes.keys(), vcf_files, outdir, sname)

            else:
                print("Using " + merged_vcf_file + "for Canvas CNV step. Improper formatting of VCF can causes errors. See "
                                                   "README for formatting tips.")

            run_canvas(args.canvas_dir, args.sorted_bam, merged_vcf_file, canvas_output_directory, removed_regions_bed,
                       sname, ref)
            args.cnv_bed = convert_canvas_cnv_to_seeds(canvas_output_directory)

        elif args.reuse_canvas:
            args.cnv_bed = convert_canvas_cnv_to_seeds(canvas_output_directory)

        elif runCNV == "CNVkit":
            cnvkit_output_directory = args.output_directory + sname + "_cnvkit_output/"
            if not os.path.exists(cnvkit_output_directory):
                os.mkdir(cnvkit_output_directory)

            run_cnvkit(args.cnvkit_dir, args.nthreads, cnvkit_output_directory, args.sorted_bam,
                       seg_meth=args.cnvkit_segmentation, normal=args.normal_bam, refG=ref)
            if args.ploidy or args.purity:
                rescale_cnvkit_calls(args.cnvkit_dir, cnvkit_output_directory, bambase, ploidy=args.ploidy,
                                     purity=args.purity)
                rescaling = True
            else:
                rescaling = False

            args.cnv_bed = convert_cnvkit_cnv_to_seeds(cnvkit_output_directory, bambase, rescaled=rescaling)

        if args.cnv_bed.endswith(".cns"):
            args.cnv_bed = convert_cnvkit_cnv_to_seeds(outdir, bambase, cnsfile=args.cnv_bed, nofilter=True)

        tb = time.time()
        logfile.write("CNV calling:\t" + "{:.2f}".format(tb - ta) + "\n")
        ta = tb

        sample_info_dict["sample_cnv_bed"] = args.cnv_bed

        if not args.no_filter and not args.cnv_bed.endswith("_AA_CNV_SEEDS.bed"):
            if not args.cnv_bed.endswith("_CNV_CALLS_pre_filtered.bed"):
                args.cnv_bed = cnv_prefilter.prefilter_bed(args.cnv_bed, args.ref, centromere_dict, chr_sizes,
                                                           args.cngain, args.output_directory)

            amplified_interval_bed = run_amplified_intervals(args.aa_python_interpreter, args.cnv_bed, args.sorted_bam,
                                                             outdir, sname, args.cngain, args.cnsize_min)

        else:
            print("Skipping filtering of bed file.")
            amplified_interval_bed = args.cnv_bed

        tb = time.time()
        logfile.write("Seed filtering (amplified_intervals.py):\t" + "{:.2f}".format(tb - ta) + "\n")
        ta = tb
        # Run AA
        if args.run_AA:
            AA_outdir = outdir + sname + "_AA_results/"
            if not os.path.exists(AA_outdir):
                os.mkdir(AA_outdir)

            run_AA(args.aa_python_interpreter, amplified_interval_bed, args.sorted_bam, AA_outdir, sname, args.downsample,
                   args.ref, args.AA_runmode, args.AA_extendmode, args.AA_insert_sdevs)
            tb = time.time()
            logfile.write("AmpliconArchitect:\t" + "{:.2f}".format(tb - ta) + "\n")
            ta = tb
            # Run AC
            if args.run_AC:
                # if 'AC_SRC' not in os.environ:
                #     sys.stderr.write("AC_SRC bash variable not found. AmpliconClassifier may not be properly installed.\n")
                # else:
                AC_SRC = os.environ['AC_SRC']
                AC_outdir = outdir + sname + "_classification/"
                if not os.path.exists(AC_outdir):
                    os.mkdir(AC_outdir)

                run_AC(AA_outdir, sname, args.ref, AC_outdir, AC_SRC)

                tb = time.time()
                logfile.write("AmpliconClassifier:\t" + "{:.2f}".format(tb - ta) + "\n")

        metadata_filename = save_run_metadata(outdir, sname, args, launchtime)
        if args.run_AA and args.run_AC:
            make_AC_table(sname, AC_outdir, AC_SRC, metadata_filename, sample_info_dict["sample_cnv_bed"])

    else:
        ta = time.time()
        AC_SRC = os.environ['AC_SRC']
        AC_outdir = outdir + sname + "_classification/"
        if not os.path.exists(AC_outdir):
            os.mkdir(AC_outdir)

        run_AC(args.completed_AA_runs, sname, args.ref, AC_outdir, AC_SRC)

        tb = time.time()
        logfile.write("AmpliconClassifier:\t" + "{:.2f}".format(tb - ta) + "\n")

        make_AC_table(sname, AC_outdir, AC_SRC, args.completed_run_metadata)
        sample_info_dict["run_metadata_file"] = args.completed_run_metadata

    sample_info_dict["reference_genome"] = args.ref
    smofname = args.output_directory + sname + "_sample_metadata.json"
    with open(smofname, 'w') as fp:
        json.dump(sample_info_dict, fp, indent=2)

    print("Completed\n")
    print(str(datetime.now()))
    tf = time.time()
    logfile.write("Total_elapsed_walltime\t" + "{:.2f}".format(tf - ti) + "\n")
    logfile.close()

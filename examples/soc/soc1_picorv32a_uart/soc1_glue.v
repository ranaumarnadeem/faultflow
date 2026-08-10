// soc1_glue.v -- SoC-level interconnect stub for assemble_soc(). AUTO-GENERATED
// (gen_soc1_glue.py) from the frozen picorv32a/simpleuart scan+wrap JSON so every
// port -- including the 162+2 per-chain scan_in_N/scan_out_N ports -- gets an
// explicit net connection, which compose_soc's net-ID remap requires. Only the
// functional interconnect (memory-mapped UART) and the WBR daisy chain carry
// real meaning; every other port gets a per-instance dummy net (each block's own
// internal functional scan is self-contained, not exposed at the chip level).
`default_nettype none

module soc1_glue (
	input  wire clk,
	input  wire resetn,

	output wire ser_tx,
	input  wire ser_rx,

	input  wire soc_wbr_se,
	input  wire soc_wbr_si,
	output wire soc_wbr_so
);

	localparam integer MEM_WORDS = 256;

	wire        mem_valid;
	wire        mem_instr;
	wire        mem_ready;
	wire [31:0] mem_addr;
	wire [31:0] mem_wdata;
	wire [ 3:0] mem_wstrb;
	wire [31:0] mem_rdata;

	// ram_ready is purely combinational: u_ram is a permanent blackbox stub
	// (see below), so there is no real access latency left to model.
	wire ram_ready = mem_valid && mem_addr < 4 * MEM_WORDS;
	wire [31:0] ram_rdata;

	wire        uart_div_sel = mem_valid && (mem_addr == 32'h 0200_0004);
	wire [31:0] uart_div_do;

	wire        uart_dat_sel = mem_valid && (mem_addr == 32'h 0200_0008);
	wire [31:0] uart_dat_do;
	wire        uart_dat_wait;

	assign mem_ready = ram_ready || uart_div_sel ||
	                    (uart_dat_sel && !uart_dat_wait);

	assign mem_rdata = ram_ready    ? ram_rdata  :
	                    uart_div_sel ? uart_div_do :
	                    uart_dat_sel ? uart_dat_do : 32'h 0000_0000;

	wire wbr_mid;

	wire [31:0] n_pv_eoi;
	wire [31:0] n_pv_mem_la_addr;
	wire n_pv_mem_la_read;
	wire [31:0] n_pv_mem_la_wdata;
	wire n_pv_mem_la_write;
	wire [3:0] n_pv_mem_la_wstrb;
	wire [31:0] n_pv_pcpi_insn;
	wire [31:0] n_pv_pcpi_rs1;
	wire [31:0] n_pv_pcpi_rs2;
	wire n_pv_pcpi_valid;
	wire n_pv_scan_en;
	wire n_pv_scan_in_0;
	wire n_pv_scan_in_1;
	wire n_pv_scan_in_10;
	wire n_pv_scan_in_100;
	wire n_pv_scan_in_101;
	wire n_pv_scan_in_102;
	wire n_pv_scan_in_103;
	wire n_pv_scan_in_104;
	wire n_pv_scan_in_105;
	wire n_pv_scan_in_106;
	wire n_pv_scan_in_107;
	wire n_pv_scan_in_108;
	wire n_pv_scan_in_109;
	wire n_pv_scan_in_11;
	wire n_pv_scan_in_110;
	wire n_pv_scan_in_111;
	wire n_pv_scan_in_112;
	wire n_pv_scan_in_113;
	wire n_pv_scan_in_114;
	wire n_pv_scan_in_115;
	wire n_pv_scan_in_116;
	wire n_pv_scan_in_117;
	wire n_pv_scan_in_118;
	wire n_pv_scan_in_119;
	wire n_pv_scan_in_12;
	wire n_pv_scan_in_120;
	wire n_pv_scan_in_121;
	wire n_pv_scan_in_122;
	wire n_pv_scan_in_123;
	wire n_pv_scan_in_124;
	wire n_pv_scan_in_125;
	wire n_pv_scan_in_126;
	wire n_pv_scan_in_127;
	wire n_pv_scan_in_128;
	wire n_pv_scan_in_129;
	wire n_pv_scan_in_13;
	wire n_pv_scan_in_130;
	wire n_pv_scan_in_131;
	wire n_pv_scan_in_132;
	wire n_pv_scan_in_133;
	wire n_pv_scan_in_134;
	wire n_pv_scan_in_135;
	wire n_pv_scan_in_136;
	wire n_pv_scan_in_137;
	wire n_pv_scan_in_138;
	wire n_pv_scan_in_139;
	wire n_pv_scan_in_14;
	wire n_pv_scan_in_140;
	wire n_pv_scan_in_141;
	wire n_pv_scan_in_142;
	wire n_pv_scan_in_143;
	wire n_pv_scan_in_144;
	wire n_pv_scan_in_145;
	wire n_pv_scan_in_146;
	wire n_pv_scan_in_147;
	wire n_pv_scan_in_148;
	wire n_pv_scan_in_149;
	wire n_pv_scan_in_15;
	wire n_pv_scan_in_150;
	wire n_pv_scan_in_151;
	wire n_pv_scan_in_152;
	wire n_pv_scan_in_153;
	wire n_pv_scan_in_154;
	wire n_pv_scan_in_155;
	wire n_pv_scan_in_156;
	wire n_pv_scan_in_157;
	wire n_pv_scan_in_158;
	wire n_pv_scan_in_159;
	wire n_pv_scan_in_16;
	wire n_pv_scan_in_160;
	wire n_pv_scan_in_161;
	wire n_pv_scan_in_17;
	wire n_pv_scan_in_18;
	wire n_pv_scan_in_19;
	wire n_pv_scan_in_2;
	wire n_pv_scan_in_20;
	wire n_pv_scan_in_21;
	wire n_pv_scan_in_22;
	wire n_pv_scan_in_23;
	wire n_pv_scan_in_24;
	wire n_pv_scan_in_25;
	wire n_pv_scan_in_26;
	wire n_pv_scan_in_27;
	wire n_pv_scan_in_28;
	wire n_pv_scan_in_29;
	wire n_pv_scan_in_3;
	wire n_pv_scan_in_30;
	wire n_pv_scan_in_31;
	wire n_pv_scan_in_32;
	wire n_pv_scan_in_33;
	wire n_pv_scan_in_34;
	wire n_pv_scan_in_35;
	wire n_pv_scan_in_36;
	wire n_pv_scan_in_37;
	wire n_pv_scan_in_38;
	wire n_pv_scan_in_39;
	wire n_pv_scan_in_4;
	wire n_pv_scan_in_40;
	wire n_pv_scan_in_41;
	wire n_pv_scan_in_42;
	wire n_pv_scan_in_43;
	wire n_pv_scan_in_44;
	wire n_pv_scan_in_45;
	wire n_pv_scan_in_46;
	wire n_pv_scan_in_47;
	wire n_pv_scan_in_48;
	wire n_pv_scan_in_49;
	wire n_pv_scan_in_5;
	wire n_pv_scan_in_50;
	wire n_pv_scan_in_51;
	wire n_pv_scan_in_52;
	wire n_pv_scan_in_53;
	wire n_pv_scan_in_54;
	wire n_pv_scan_in_55;
	wire n_pv_scan_in_56;
	wire n_pv_scan_in_57;
	wire n_pv_scan_in_58;
	wire n_pv_scan_in_59;
	wire n_pv_scan_in_6;
	wire n_pv_scan_in_60;
	wire n_pv_scan_in_61;
	wire n_pv_scan_in_62;
	wire n_pv_scan_in_63;
	wire n_pv_scan_in_64;
	wire n_pv_scan_in_65;
	wire n_pv_scan_in_66;
	wire n_pv_scan_in_67;
	wire n_pv_scan_in_68;
	wire n_pv_scan_in_69;
	wire n_pv_scan_in_7;
	wire n_pv_scan_in_70;
	wire n_pv_scan_in_71;
	wire n_pv_scan_in_72;
	wire n_pv_scan_in_73;
	wire n_pv_scan_in_74;
	wire n_pv_scan_in_75;
	wire n_pv_scan_in_76;
	wire n_pv_scan_in_77;
	wire n_pv_scan_in_78;
	wire n_pv_scan_in_79;
	wire n_pv_scan_in_8;
	wire n_pv_scan_in_80;
	wire n_pv_scan_in_81;
	wire n_pv_scan_in_82;
	wire n_pv_scan_in_83;
	wire n_pv_scan_in_84;
	wire n_pv_scan_in_85;
	wire n_pv_scan_in_86;
	wire n_pv_scan_in_87;
	wire n_pv_scan_in_88;
	wire n_pv_scan_in_89;
	wire n_pv_scan_in_9;
	wire n_pv_scan_in_90;
	wire n_pv_scan_in_91;
	wire n_pv_scan_in_92;
	wire n_pv_scan_in_93;
	wire n_pv_scan_in_94;
	wire n_pv_scan_in_95;
	wire n_pv_scan_in_96;
	wire n_pv_scan_in_97;
	wire n_pv_scan_in_98;
	wire n_pv_scan_in_99;
	wire n_pv_scan_out_0;
	wire n_pv_scan_out_1;
	wire n_pv_scan_out_10;
	wire n_pv_scan_out_100;
	wire n_pv_scan_out_101;
	wire n_pv_scan_out_102;
	wire n_pv_scan_out_103;
	wire n_pv_scan_out_104;
	wire n_pv_scan_out_105;
	wire n_pv_scan_out_106;
	wire n_pv_scan_out_107;
	wire n_pv_scan_out_108;
	wire n_pv_scan_out_109;
	wire n_pv_scan_out_11;
	wire n_pv_scan_out_110;
	wire n_pv_scan_out_111;
	wire n_pv_scan_out_112;
	wire n_pv_scan_out_113;
	wire n_pv_scan_out_114;
	wire n_pv_scan_out_115;
	wire n_pv_scan_out_116;
	wire n_pv_scan_out_117;
	wire n_pv_scan_out_118;
	wire n_pv_scan_out_119;
	wire n_pv_scan_out_12;
	wire n_pv_scan_out_120;
	wire n_pv_scan_out_121;
	wire n_pv_scan_out_122;
	wire n_pv_scan_out_123;
	wire n_pv_scan_out_124;
	wire n_pv_scan_out_125;
	wire n_pv_scan_out_126;
	wire n_pv_scan_out_127;
	wire n_pv_scan_out_128;
	wire n_pv_scan_out_129;
	wire n_pv_scan_out_13;
	wire n_pv_scan_out_130;
	wire n_pv_scan_out_131;
	wire n_pv_scan_out_132;
	wire n_pv_scan_out_133;
	wire n_pv_scan_out_134;
	wire n_pv_scan_out_135;
	wire n_pv_scan_out_136;
	wire n_pv_scan_out_137;
	wire n_pv_scan_out_138;
	wire n_pv_scan_out_139;
	wire n_pv_scan_out_14;
	wire n_pv_scan_out_140;
	wire n_pv_scan_out_141;
	wire n_pv_scan_out_142;
	wire n_pv_scan_out_143;
	wire n_pv_scan_out_144;
	wire n_pv_scan_out_145;
	wire n_pv_scan_out_146;
	wire n_pv_scan_out_147;
	wire n_pv_scan_out_148;
	wire n_pv_scan_out_149;
	wire n_pv_scan_out_15;
	wire n_pv_scan_out_150;
	wire n_pv_scan_out_151;
	wire n_pv_scan_out_152;
	wire n_pv_scan_out_153;
	wire n_pv_scan_out_154;
	wire n_pv_scan_out_155;
	wire n_pv_scan_out_156;
	wire n_pv_scan_out_157;
	wire n_pv_scan_out_158;
	wire n_pv_scan_out_159;
	wire n_pv_scan_out_16;
	wire n_pv_scan_out_160;
	wire n_pv_scan_out_161;
	wire n_pv_scan_out_17;
	wire n_pv_scan_out_18;
	wire n_pv_scan_out_19;
	wire n_pv_scan_out_2;
	wire n_pv_scan_out_20;
	wire n_pv_scan_out_21;
	wire n_pv_scan_out_22;
	wire n_pv_scan_out_23;
	wire n_pv_scan_out_24;
	wire n_pv_scan_out_25;
	wire n_pv_scan_out_26;
	wire n_pv_scan_out_27;
	wire n_pv_scan_out_28;
	wire n_pv_scan_out_29;
	wire n_pv_scan_out_3;
	wire n_pv_scan_out_30;
	wire n_pv_scan_out_31;
	wire n_pv_scan_out_32;
	wire n_pv_scan_out_33;
	wire n_pv_scan_out_34;
	wire n_pv_scan_out_35;
	wire n_pv_scan_out_36;
	wire n_pv_scan_out_37;
	wire n_pv_scan_out_38;
	wire n_pv_scan_out_39;
	wire n_pv_scan_out_4;
	wire n_pv_scan_out_40;
	wire n_pv_scan_out_41;
	wire n_pv_scan_out_42;
	wire n_pv_scan_out_43;
	wire n_pv_scan_out_44;
	wire n_pv_scan_out_45;
	wire n_pv_scan_out_46;
	wire n_pv_scan_out_47;
	wire n_pv_scan_out_48;
	wire n_pv_scan_out_49;
	wire n_pv_scan_out_5;
	wire n_pv_scan_out_50;
	wire n_pv_scan_out_51;
	wire n_pv_scan_out_52;
	wire n_pv_scan_out_53;
	wire n_pv_scan_out_54;
	wire n_pv_scan_out_55;
	wire n_pv_scan_out_56;
	wire n_pv_scan_out_57;
	wire n_pv_scan_out_58;
	wire n_pv_scan_out_59;
	wire n_pv_scan_out_6;
	wire n_pv_scan_out_60;
	wire n_pv_scan_out_61;
	wire n_pv_scan_out_62;
	wire n_pv_scan_out_63;
	wire n_pv_scan_out_64;
	wire n_pv_scan_out_65;
	wire n_pv_scan_out_66;
	wire n_pv_scan_out_67;
	wire n_pv_scan_out_68;
	wire n_pv_scan_out_69;
	wire n_pv_scan_out_7;
	wire n_pv_scan_out_70;
	wire n_pv_scan_out_71;
	wire n_pv_scan_out_72;
	wire n_pv_scan_out_73;
	wire n_pv_scan_out_74;
	wire n_pv_scan_out_75;
	wire n_pv_scan_out_76;
	wire n_pv_scan_out_77;
	wire n_pv_scan_out_78;
	wire n_pv_scan_out_79;
	wire n_pv_scan_out_8;
	wire n_pv_scan_out_80;
	wire n_pv_scan_out_81;
	wire n_pv_scan_out_82;
	wire n_pv_scan_out_83;
	wire n_pv_scan_out_84;
	wire n_pv_scan_out_85;
	wire n_pv_scan_out_86;
	wire n_pv_scan_out_87;
	wire n_pv_scan_out_88;
	wire n_pv_scan_out_89;
	wire n_pv_scan_out_9;
	wire n_pv_scan_out_90;
	wire n_pv_scan_out_91;
	wire n_pv_scan_out_92;
	wire n_pv_scan_out_93;
	wire n_pv_scan_out_94;
	wire n_pv_scan_out_95;
	wire n_pv_scan_out_96;
	wire n_pv_scan_out_97;
	wire n_pv_scan_out_98;
	wire n_pv_scan_out_99;
	wire [35:0] n_pv_trace_data;
	wire n_pv_trace_valid;
	wire n_pv_trap;

	picorv32a u_picorv32a (
		.clk (clk),
		.eoi (n_pv_eoi),
		.irq (32'b0),
		.mem_addr (mem_addr),
		.mem_instr (mem_instr),
		.mem_la_addr (n_pv_mem_la_addr),
		.mem_la_read (n_pv_mem_la_read),
		.mem_la_wdata (n_pv_mem_la_wdata),
		.mem_la_write (n_pv_mem_la_write),
		.mem_la_wstrb (n_pv_mem_la_wstrb),
		.mem_rdata (mem_rdata),
		.mem_ready (mem_ready),
		.mem_valid (mem_valid),
		.mem_wdata (mem_wdata),
		.mem_wstrb (mem_wstrb),
		.pcpi_insn (n_pv_pcpi_insn),
		.pcpi_rd (32'b0),
		.pcpi_ready (1'b0),
		.pcpi_rs1 (n_pv_pcpi_rs1),
		.pcpi_rs2 (n_pv_pcpi_rs2),
		.pcpi_valid (n_pv_pcpi_valid),
		.pcpi_wait (1'b0),
		.pcpi_wr (1'b0),
		.resetn (resetn),
		.scan_en (n_pv_scan_en),
		.scan_in_0 (n_pv_scan_in_0),
		.scan_in_1 (n_pv_scan_in_1),
		.scan_in_10 (n_pv_scan_in_10),
		.scan_in_100 (n_pv_scan_in_100),
		.scan_in_101 (n_pv_scan_in_101),
		.scan_in_102 (n_pv_scan_in_102),
		.scan_in_103 (n_pv_scan_in_103),
		.scan_in_104 (n_pv_scan_in_104),
		.scan_in_105 (n_pv_scan_in_105),
		.scan_in_106 (n_pv_scan_in_106),
		.scan_in_107 (n_pv_scan_in_107),
		.scan_in_108 (n_pv_scan_in_108),
		.scan_in_109 (n_pv_scan_in_109),
		.scan_in_11 (n_pv_scan_in_11),
		.scan_in_110 (n_pv_scan_in_110),
		.scan_in_111 (n_pv_scan_in_111),
		.scan_in_112 (n_pv_scan_in_112),
		.scan_in_113 (n_pv_scan_in_113),
		.scan_in_114 (n_pv_scan_in_114),
		.scan_in_115 (n_pv_scan_in_115),
		.scan_in_116 (n_pv_scan_in_116),
		.scan_in_117 (n_pv_scan_in_117),
		.scan_in_118 (n_pv_scan_in_118),
		.scan_in_119 (n_pv_scan_in_119),
		.scan_in_12 (n_pv_scan_in_12),
		.scan_in_120 (n_pv_scan_in_120),
		.scan_in_121 (n_pv_scan_in_121),
		.scan_in_122 (n_pv_scan_in_122),
		.scan_in_123 (n_pv_scan_in_123),
		.scan_in_124 (n_pv_scan_in_124),
		.scan_in_125 (n_pv_scan_in_125),
		.scan_in_126 (n_pv_scan_in_126),
		.scan_in_127 (n_pv_scan_in_127),
		.scan_in_128 (n_pv_scan_in_128),
		.scan_in_129 (n_pv_scan_in_129),
		.scan_in_13 (n_pv_scan_in_13),
		.scan_in_130 (n_pv_scan_in_130),
		.scan_in_131 (n_pv_scan_in_131),
		.scan_in_132 (n_pv_scan_in_132),
		.scan_in_133 (n_pv_scan_in_133),
		.scan_in_134 (n_pv_scan_in_134),
		.scan_in_135 (n_pv_scan_in_135),
		.scan_in_136 (n_pv_scan_in_136),
		.scan_in_137 (n_pv_scan_in_137),
		.scan_in_138 (n_pv_scan_in_138),
		.scan_in_139 (n_pv_scan_in_139),
		.scan_in_14 (n_pv_scan_in_14),
		.scan_in_140 (n_pv_scan_in_140),
		.scan_in_141 (n_pv_scan_in_141),
		.scan_in_142 (n_pv_scan_in_142),
		.scan_in_143 (n_pv_scan_in_143),
		.scan_in_144 (n_pv_scan_in_144),
		.scan_in_145 (n_pv_scan_in_145),
		.scan_in_146 (n_pv_scan_in_146),
		.scan_in_147 (n_pv_scan_in_147),
		.scan_in_148 (n_pv_scan_in_148),
		.scan_in_149 (n_pv_scan_in_149),
		.scan_in_15 (n_pv_scan_in_15),
		.scan_in_150 (n_pv_scan_in_150),
		.scan_in_151 (n_pv_scan_in_151),
		.scan_in_152 (n_pv_scan_in_152),
		.scan_in_153 (n_pv_scan_in_153),
		.scan_in_154 (n_pv_scan_in_154),
		.scan_in_155 (n_pv_scan_in_155),
		.scan_in_156 (n_pv_scan_in_156),
		.scan_in_157 (n_pv_scan_in_157),
		.scan_in_158 (n_pv_scan_in_158),
		.scan_in_159 (n_pv_scan_in_159),
		.scan_in_16 (n_pv_scan_in_16),
		.scan_in_160 (n_pv_scan_in_160),
		.scan_in_161 (n_pv_scan_in_161),
		.scan_in_17 (n_pv_scan_in_17),
		.scan_in_18 (n_pv_scan_in_18),
		.scan_in_19 (n_pv_scan_in_19),
		.scan_in_2 (n_pv_scan_in_2),
		.scan_in_20 (n_pv_scan_in_20),
		.scan_in_21 (n_pv_scan_in_21),
		.scan_in_22 (n_pv_scan_in_22),
		.scan_in_23 (n_pv_scan_in_23),
		.scan_in_24 (n_pv_scan_in_24),
		.scan_in_25 (n_pv_scan_in_25),
		.scan_in_26 (n_pv_scan_in_26),
		.scan_in_27 (n_pv_scan_in_27),
		.scan_in_28 (n_pv_scan_in_28),
		.scan_in_29 (n_pv_scan_in_29),
		.scan_in_3 (n_pv_scan_in_3),
		.scan_in_30 (n_pv_scan_in_30),
		.scan_in_31 (n_pv_scan_in_31),
		.scan_in_32 (n_pv_scan_in_32),
		.scan_in_33 (n_pv_scan_in_33),
		.scan_in_34 (n_pv_scan_in_34),
		.scan_in_35 (n_pv_scan_in_35),
		.scan_in_36 (n_pv_scan_in_36),
		.scan_in_37 (n_pv_scan_in_37),
		.scan_in_38 (n_pv_scan_in_38),
		.scan_in_39 (n_pv_scan_in_39),
		.scan_in_4 (n_pv_scan_in_4),
		.scan_in_40 (n_pv_scan_in_40),
		.scan_in_41 (n_pv_scan_in_41),
		.scan_in_42 (n_pv_scan_in_42),
		.scan_in_43 (n_pv_scan_in_43),
		.scan_in_44 (n_pv_scan_in_44),
		.scan_in_45 (n_pv_scan_in_45),
		.scan_in_46 (n_pv_scan_in_46),
		.scan_in_47 (n_pv_scan_in_47),
		.scan_in_48 (n_pv_scan_in_48),
		.scan_in_49 (n_pv_scan_in_49),
		.scan_in_5 (n_pv_scan_in_5),
		.scan_in_50 (n_pv_scan_in_50),
		.scan_in_51 (n_pv_scan_in_51),
		.scan_in_52 (n_pv_scan_in_52),
		.scan_in_53 (n_pv_scan_in_53),
		.scan_in_54 (n_pv_scan_in_54),
		.scan_in_55 (n_pv_scan_in_55),
		.scan_in_56 (n_pv_scan_in_56),
		.scan_in_57 (n_pv_scan_in_57),
		.scan_in_58 (n_pv_scan_in_58),
		.scan_in_59 (n_pv_scan_in_59),
		.scan_in_6 (n_pv_scan_in_6),
		.scan_in_60 (n_pv_scan_in_60),
		.scan_in_61 (n_pv_scan_in_61),
		.scan_in_62 (n_pv_scan_in_62),
		.scan_in_63 (n_pv_scan_in_63),
		.scan_in_64 (n_pv_scan_in_64),
		.scan_in_65 (n_pv_scan_in_65),
		.scan_in_66 (n_pv_scan_in_66),
		.scan_in_67 (n_pv_scan_in_67),
		.scan_in_68 (n_pv_scan_in_68),
		.scan_in_69 (n_pv_scan_in_69),
		.scan_in_7 (n_pv_scan_in_7),
		.scan_in_70 (n_pv_scan_in_70),
		.scan_in_71 (n_pv_scan_in_71),
		.scan_in_72 (n_pv_scan_in_72),
		.scan_in_73 (n_pv_scan_in_73),
		.scan_in_74 (n_pv_scan_in_74),
		.scan_in_75 (n_pv_scan_in_75),
		.scan_in_76 (n_pv_scan_in_76),
		.scan_in_77 (n_pv_scan_in_77),
		.scan_in_78 (n_pv_scan_in_78),
		.scan_in_79 (n_pv_scan_in_79),
		.scan_in_8 (n_pv_scan_in_8),
		.scan_in_80 (n_pv_scan_in_80),
		.scan_in_81 (n_pv_scan_in_81),
		.scan_in_82 (n_pv_scan_in_82),
		.scan_in_83 (n_pv_scan_in_83),
		.scan_in_84 (n_pv_scan_in_84),
		.scan_in_85 (n_pv_scan_in_85),
		.scan_in_86 (n_pv_scan_in_86),
		.scan_in_87 (n_pv_scan_in_87),
		.scan_in_88 (n_pv_scan_in_88),
		.scan_in_89 (n_pv_scan_in_89),
		.scan_in_9 (n_pv_scan_in_9),
		.scan_in_90 (n_pv_scan_in_90),
		.scan_in_91 (n_pv_scan_in_91),
		.scan_in_92 (n_pv_scan_in_92),
		.scan_in_93 (n_pv_scan_in_93),
		.scan_in_94 (n_pv_scan_in_94),
		.scan_in_95 (n_pv_scan_in_95),
		.scan_in_96 (n_pv_scan_in_96),
		.scan_in_97 (n_pv_scan_in_97),
		.scan_in_98 (n_pv_scan_in_98),
		.scan_in_99 (n_pv_scan_in_99),
		.scan_out_0 (n_pv_scan_out_0),
		.scan_out_1 (n_pv_scan_out_1),
		.scan_out_10 (n_pv_scan_out_10),
		.scan_out_100 (n_pv_scan_out_100),
		.scan_out_101 (n_pv_scan_out_101),
		.scan_out_102 (n_pv_scan_out_102),
		.scan_out_103 (n_pv_scan_out_103),
		.scan_out_104 (n_pv_scan_out_104),
		.scan_out_105 (n_pv_scan_out_105),
		.scan_out_106 (n_pv_scan_out_106),
		.scan_out_107 (n_pv_scan_out_107),
		.scan_out_108 (n_pv_scan_out_108),
		.scan_out_109 (n_pv_scan_out_109),
		.scan_out_11 (n_pv_scan_out_11),
		.scan_out_110 (n_pv_scan_out_110),
		.scan_out_111 (n_pv_scan_out_111),
		.scan_out_112 (n_pv_scan_out_112),
		.scan_out_113 (n_pv_scan_out_113),
		.scan_out_114 (n_pv_scan_out_114),
		.scan_out_115 (n_pv_scan_out_115),
		.scan_out_116 (n_pv_scan_out_116),
		.scan_out_117 (n_pv_scan_out_117),
		.scan_out_118 (n_pv_scan_out_118),
		.scan_out_119 (n_pv_scan_out_119),
		.scan_out_12 (n_pv_scan_out_12),
		.scan_out_120 (n_pv_scan_out_120),
		.scan_out_121 (n_pv_scan_out_121),
		.scan_out_122 (n_pv_scan_out_122),
		.scan_out_123 (n_pv_scan_out_123),
		.scan_out_124 (n_pv_scan_out_124),
		.scan_out_125 (n_pv_scan_out_125),
		.scan_out_126 (n_pv_scan_out_126),
		.scan_out_127 (n_pv_scan_out_127),
		.scan_out_128 (n_pv_scan_out_128),
		.scan_out_129 (n_pv_scan_out_129),
		.scan_out_13 (n_pv_scan_out_13),
		.scan_out_130 (n_pv_scan_out_130),
		.scan_out_131 (n_pv_scan_out_131),
		.scan_out_132 (n_pv_scan_out_132),
		.scan_out_133 (n_pv_scan_out_133),
		.scan_out_134 (n_pv_scan_out_134),
		.scan_out_135 (n_pv_scan_out_135),
		.scan_out_136 (n_pv_scan_out_136),
		.scan_out_137 (n_pv_scan_out_137),
		.scan_out_138 (n_pv_scan_out_138),
		.scan_out_139 (n_pv_scan_out_139),
		.scan_out_14 (n_pv_scan_out_14),
		.scan_out_140 (n_pv_scan_out_140),
		.scan_out_141 (n_pv_scan_out_141),
		.scan_out_142 (n_pv_scan_out_142),
		.scan_out_143 (n_pv_scan_out_143),
		.scan_out_144 (n_pv_scan_out_144),
		.scan_out_145 (n_pv_scan_out_145),
		.scan_out_146 (n_pv_scan_out_146),
		.scan_out_147 (n_pv_scan_out_147),
		.scan_out_148 (n_pv_scan_out_148),
		.scan_out_149 (n_pv_scan_out_149),
		.scan_out_15 (n_pv_scan_out_15),
		.scan_out_150 (n_pv_scan_out_150),
		.scan_out_151 (n_pv_scan_out_151),
		.scan_out_152 (n_pv_scan_out_152),
		.scan_out_153 (n_pv_scan_out_153),
		.scan_out_154 (n_pv_scan_out_154),
		.scan_out_155 (n_pv_scan_out_155),
		.scan_out_156 (n_pv_scan_out_156),
		.scan_out_157 (n_pv_scan_out_157),
		.scan_out_158 (n_pv_scan_out_158),
		.scan_out_159 (n_pv_scan_out_159),
		.scan_out_16 (n_pv_scan_out_16),
		.scan_out_160 (n_pv_scan_out_160),
		.scan_out_161 (n_pv_scan_out_161),
		.scan_out_17 (n_pv_scan_out_17),
		.scan_out_18 (n_pv_scan_out_18),
		.scan_out_19 (n_pv_scan_out_19),
		.scan_out_2 (n_pv_scan_out_2),
		.scan_out_20 (n_pv_scan_out_20),
		.scan_out_21 (n_pv_scan_out_21),
		.scan_out_22 (n_pv_scan_out_22),
		.scan_out_23 (n_pv_scan_out_23),
		.scan_out_24 (n_pv_scan_out_24),
		.scan_out_25 (n_pv_scan_out_25),
		.scan_out_26 (n_pv_scan_out_26),
		.scan_out_27 (n_pv_scan_out_27),
		.scan_out_28 (n_pv_scan_out_28),
		.scan_out_29 (n_pv_scan_out_29),
		.scan_out_3 (n_pv_scan_out_3),
		.scan_out_30 (n_pv_scan_out_30),
		.scan_out_31 (n_pv_scan_out_31),
		.scan_out_32 (n_pv_scan_out_32),
		.scan_out_33 (n_pv_scan_out_33),
		.scan_out_34 (n_pv_scan_out_34),
		.scan_out_35 (n_pv_scan_out_35),
		.scan_out_36 (n_pv_scan_out_36),
		.scan_out_37 (n_pv_scan_out_37),
		.scan_out_38 (n_pv_scan_out_38),
		.scan_out_39 (n_pv_scan_out_39),
		.scan_out_4 (n_pv_scan_out_4),
		.scan_out_40 (n_pv_scan_out_40),
		.scan_out_41 (n_pv_scan_out_41),
		.scan_out_42 (n_pv_scan_out_42),
		.scan_out_43 (n_pv_scan_out_43),
		.scan_out_44 (n_pv_scan_out_44),
		.scan_out_45 (n_pv_scan_out_45),
		.scan_out_46 (n_pv_scan_out_46),
		.scan_out_47 (n_pv_scan_out_47),
		.scan_out_48 (n_pv_scan_out_48),
		.scan_out_49 (n_pv_scan_out_49),
		.scan_out_5 (n_pv_scan_out_5),
		.scan_out_50 (n_pv_scan_out_50),
		.scan_out_51 (n_pv_scan_out_51),
		.scan_out_52 (n_pv_scan_out_52),
		.scan_out_53 (n_pv_scan_out_53),
		.scan_out_54 (n_pv_scan_out_54),
		.scan_out_55 (n_pv_scan_out_55),
		.scan_out_56 (n_pv_scan_out_56),
		.scan_out_57 (n_pv_scan_out_57),
		.scan_out_58 (n_pv_scan_out_58),
		.scan_out_59 (n_pv_scan_out_59),
		.scan_out_6 (n_pv_scan_out_6),
		.scan_out_60 (n_pv_scan_out_60),
		.scan_out_61 (n_pv_scan_out_61),
		.scan_out_62 (n_pv_scan_out_62),
		.scan_out_63 (n_pv_scan_out_63),
		.scan_out_64 (n_pv_scan_out_64),
		.scan_out_65 (n_pv_scan_out_65),
		.scan_out_66 (n_pv_scan_out_66),
		.scan_out_67 (n_pv_scan_out_67),
		.scan_out_68 (n_pv_scan_out_68),
		.scan_out_69 (n_pv_scan_out_69),
		.scan_out_7 (n_pv_scan_out_7),
		.scan_out_70 (n_pv_scan_out_70),
		.scan_out_71 (n_pv_scan_out_71),
		.scan_out_72 (n_pv_scan_out_72),
		.scan_out_73 (n_pv_scan_out_73),
		.scan_out_74 (n_pv_scan_out_74),
		.scan_out_75 (n_pv_scan_out_75),
		.scan_out_76 (n_pv_scan_out_76),
		.scan_out_77 (n_pv_scan_out_77),
		.scan_out_78 (n_pv_scan_out_78),
		.scan_out_79 (n_pv_scan_out_79),
		.scan_out_8 (n_pv_scan_out_8),
		.scan_out_80 (n_pv_scan_out_80),
		.scan_out_81 (n_pv_scan_out_81),
		.scan_out_82 (n_pv_scan_out_82),
		.scan_out_83 (n_pv_scan_out_83),
		.scan_out_84 (n_pv_scan_out_84),
		.scan_out_85 (n_pv_scan_out_85),
		.scan_out_86 (n_pv_scan_out_86),
		.scan_out_87 (n_pv_scan_out_87),
		.scan_out_88 (n_pv_scan_out_88),
		.scan_out_89 (n_pv_scan_out_89),
		.scan_out_9 (n_pv_scan_out_9),
		.scan_out_90 (n_pv_scan_out_90),
		.scan_out_91 (n_pv_scan_out_91),
		.scan_out_92 (n_pv_scan_out_92),
		.scan_out_93 (n_pv_scan_out_93),
		.scan_out_94 (n_pv_scan_out_94),
		.scan_out_95 (n_pv_scan_out_95),
		.scan_out_96 (n_pv_scan_out_96),
		.scan_out_97 (n_pv_scan_out_97),
		.scan_out_98 (n_pv_scan_out_98),
		.scan_out_99 (n_pv_scan_out_99),
		.trace_data (n_pv_trace_data),
		.trace_valid (n_pv_trace_valid),
		.trap (n_pv_trap),
		.wbr_se (soc_wbr_se),
		.wbr_si (soc_wbr_si),
		.wbr_so (wbr_mid)
	);

	wire n_ua_scan_en;
	wire n_ua_scan_in_0;
	wire n_ua_scan_in_1;
	wire n_ua_scan_out_0;
	wire n_ua_scan_out_1;

	simpleuart u_simpleuart (
		.clk (clk),
		.reg_dat_di (mem_wdata),
		.reg_dat_do (uart_dat_do),
		.reg_dat_re (uart_dat_sel && !mem_wstrb),
		.reg_dat_wait (uart_dat_wait),
		.reg_dat_we (uart_dat_sel ? mem_wstrb[0] : 1'b0),
		.reg_div_di (mem_wdata),
		.reg_div_do (uart_div_do),
		.reg_div_we (uart_div_sel ? mem_wstrb : 4'b0000),
		.resetn (resetn),
		.scan_en (n_ua_scan_en),
		.scan_in_0 (n_ua_scan_in_0),
		.scan_in_1 (n_ua_scan_in_1),
		.scan_out_0 (n_ua_scan_out_0),
		.scan_out_1 (n_ua_scan_out_1),
		.ser_rx (ser_rx),
		.ser_tx (ser_tx),
		.wbr_se (soc_wbr_se),
		.wbr_si (wbr_mid),
		.wbr_so (soc_wbr_so)
	);

	soc1_ram #(
		.WORDS (MEM_WORDS)
	) u_ram (
		.clk   (clk),
		.wen   ((mem_valid && !mem_ready && mem_addr < 4 * MEM_WORDS) ? mem_wstrb : 4'b0),
		.addr  (mem_addr[23:2]),
		.wdata (mem_wdata),
		.rdata (ram_rdata)
	);

endmodule

// Permanent blackbox stub for the on-chip RAM -- see soc1_ram.v's header
// comment for why (256x32 = 8,192 real FFs; blackboxing via faultflow's
// existing Policy 1 mechanism, add_blackbox / blackbox_instances, is the
// methodologically honest treatment, matching real DFT flows that hand
// embedded memories to dedicated BIST rather than logic ATPG). No real body
// is defined anywhere else read during synthesis, so every instance stays
// one opaque, unexpanded top-level cell for blackbox_instances to match.
(* blackbox *)
module soc1_ram #(
	parameter integer WORDS = 256
) (
	input  wire        clk,
	input  wire [ 3:0] wen,
	input  wire [21:0] addr,
	input  wire [31:0] wdata,
	output wire [31:0] rdata
);
endmodule

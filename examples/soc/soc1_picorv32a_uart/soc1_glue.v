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
	output wire soc_wbr_so,

	input  wire picorv32a_scan_in,
	output wire picorv32a_scan_out,
	input  wire uart_scan_in,
	output wire uart_scan_out
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
	wire [35:0] n_pv_trace_data;
	wire n_pv_trace_valid;
	wire n_pv_trap;

	wire pico_scan_en_tie = soc_wbr_se;
	wire pico_scanlink_1;
	wire pico_scanlink_2;
	wire pico_scanlink_3;
	wire pico_scanlink_4;
	wire pico_scanlink_5;
	wire pico_scanlink_6;
	wire pico_scanlink_7;
	wire pico_scanlink_8;
	wire pico_scanlink_9;
	wire pico_scanlink_10;
	wire pico_scanlink_11;
	wire pico_scanlink_12;
	wire pico_scanlink_13;
	wire pico_scanlink_14;
	wire pico_scanlink_15;
	wire pico_scanlink_16;
	wire pico_scanlink_17;
	wire pico_scanlink_18;
	wire pico_scanlink_19;
	wire pico_scanlink_20;
	wire pico_scanlink_21;
	wire pico_scanlink_22;
	wire pico_scanlink_23;
	wire pico_scanlink_24;
	wire pico_scanlink_25;
	wire pico_scanlink_26;
	wire pico_scanlink_27;
	wire pico_scanlink_28;
	wire pico_scanlink_29;
	wire pico_scanlink_30;
	wire pico_scanlink_31;
	wire pico_scanlink_32;
	wire pico_scanlink_33;
	wire pico_scanlink_34;
	wire pico_scanlink_35;
	wire pico_scanlink_36;
	wire pico_scanlink_37;
	wire pico_scanlink_38;
	wire pico_scanlink_39;
	wire pico_scanlink_40;
	wire pico_scanlink_41;
	wire pico_scanlink_42;
	wire pico_scanlink_43;
	wire pico_scanlink_44;
	wire pico_scanlink_45;
	wire pico_scanlink_46;
	wire pico_scanlink_47;
	wire pico_scanlink_48;
	wire pico_scanlink_49;
	wire pico_scanlink_50;
	wire pico_scanlink_51;
	wire pico_scanlink_52;
	wire pico_scanlink_53;
	wire pico_scanlink_54;
	wire pico_scanlink_55;
	wire pico_scanlink_56;
	wire pico_scanlink_57;
	wire pico_scanlink_58;
	wire pico_scanlink_59;
	wire pico_scanlink_60;
	wire pico_scanlink_61;
	wire pico_scanlink_62;
	wire pico_scanlink_63;
	wire pico_scanlink_64;
	wire pico_scanlink_65;
	wire pico_scanlink_66;
	wire pico_scanlink_67;
	wire pico_scanlink_68;
	wire pico_scanlink_69;
	wire pico_scanlink_70;
	wire pico_scanlink_71;
	wire pico_scanlink_72;
	wire pico_scanlink_73;
	wire pico_scanlink_74;
	wire pico_scanlink_75;
	wire pico_scanlink_76;
	wire pico_scanlink_77;
	wire pico_scanlink_78;
	wire pico_scanlink_79;
	wire pico_scanlink_80;
	wire pico_scanlink_81;
	wire pico_scanlink_82;
	wire pico_scanlink_83;
	wire pico_scanlink_84;
	wire pico_scanlink_85;
	wire pico_scanlink_86;
	wire pico_scanlink_87;
	wire pico_scanlink_88;
	wire pico_scanlink_89;
	wire pico_scanlink_90;
	wire pico_scanlink_91;
	wire pico_scanlink_92;
	wire pico_scanlink_93;
	wire pico_scanlink_94;
	wire pico_scanlink_95;
	wire pico_scanlink_96;
	wire pico_scanlink_97;
	wire pico_scanlink_98;
	wire pico_scanlink_99;
	wire pico_scanlink_100;
	wire pico_scanlink_101;
	wire pico_scanlink_102;
	wire pico_scanlink_103;
	wire pico_scanlink_104;
	wire pico_scanlink_105;
	wire pico_scanlink_106;
	wire pico_scanlink_107;
	wire pico_scanlink_108;
	wire pico_scanlink_109;
	wire pico_scanlink_110;
	wire pico_scanlink_111;
	wire pico_scanlink_112;
	wire pico_scanlink_113;
	wire pico_scanlink_114;
	wire pico_scanlink_115;
	wire pico_scanlink_116;
	wire pico_scanlink_117;
	wire pico_scanlink_118;
	wire pico_scanlink_119;
	wire pico_scanlink_120;
	wire pico_scanlink_121;
	wire pico_scanlink_122;
	wire pico_scanlink_123;
	wire pico_scanlink_124;
	wire pico_scanlink_125;
	wire pico_scanlink_126;
	wire pico_scanlink_127;
	wire pico_scanlink_128;
	wire pico_scanlink_129;
	wire pico_scanlink_130;
	wire pico_scanlink_131;
	wire pico_scanlink_132;
	wire pico_scanlink_133;
	wire pico_scanlink_134;
	wire pico_scanlink_135;
	wire pico_scanlink_136;
	wire pico_scanlink_137;
	wire pico_scanlink_138;
	wire pico_scanlink_139;
	wire pico_scanlink_140;
	wire pico_scanlink_141;
	wire pico_scanlink_142;
	wire pico_scanlink_143;
	wire pico_scanlink_144;
	wire pico_scanlink_145;
	wire pico_scanlink_146;
	wire pico_scanlink_147;
	wire pico_scanlink_148;
	wire pico_scanlink_149;
	wire pico_scanlink_150;
	wire pico_scanlink_151;
	wire pico_scanlink_152;
	wire pico_scanlink_153;
	wire pico_scanlink_154;
	wire pico_scanlink_155;
	wire pico_scanlink_156;
	wire pico_scanlink_157;
	wire pico_scanlink_158;
	wire pico_scanlink_159;
	wire pico_scanlink_160;
	wire pico_scanlink_161;

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
		.trace_data (n_pv_trace_data),
		.trace_valid (n_pv_trace_valid),
		.trap (n_pv_trap),
		.scan_en (pico_scan_en_tie),
		.scan_in_0 (picorv32a_scan_in),
		.scan_out_0 (pico_scanlink_1),
		.scan_in_1 (pico_scanlink_1),
		.scan_out_1 (pico_scanlink_2),
		.scan_in_2 (pico_scanlink_2),
		.scan_out_2 (pico_scanlink_3),
		.scan_in_3 (pico_scanlink_3),
		.scan_out_3 (pico_scanlink_4),
		.scan_in_4 (pico_scanlink_4),
		.scan_out_4 (pico_scanlink_5),
		.scan_in_5 (pico_scanlink_5),
		.scan_out_5 (pico_scanlink_6),
		.scan_in_6 (pico_scanlink_6),
		.scan_out_6 (pico_scanlink_7),
		.scan_in_7 (pico_scanlink_7),
		.scan_out_7 (pico_scanlink_8),
		.scan_in_8 (pico_scanlink_8),
		.scan_out_8 (pico_scanlink_9),
		.scan_in_9 (pico_scanlink_9),
		.scan_out_9 (pico_scanlink_10),
		.scan_in_10 (pico_scanlink_10),
		.scan_out_10 (pico_scanlink_11),
		.scan_in_11 (pico_scanlink_11),
		.scan_out_11 (pico_scanlink_12),
		.scan_in_12 (pico_scanlink_12),
		.scan_out_12 (pico_scanlink_13),
		.scan_in_13 (pico_scanlink_13),
		.scan_out_13 (pico_scanlink_14),
		.scan_in_14 (pico_scanlink_14),
		.scan_out_14 (pico_scanlink_15),
		.scan_in_15 (pico_scanlink_15),
		.scan_out_15 (pico_scanlink_16),
		.scan_in_16 (pico_scanlink_16),
		.scan_out_16 (pico_scanlink_17),
		.scan_in_17 (pico_scanlink_17),
		.scan_out_17 (pico_scanlink_18),
		.scan_in_18 (pico_scanlink_18),
		.scan_out_18 (pico_scanlink_19),
		.scan_in_19 (pico_scanlink_19),
		.scan_out_19 (pico_scanlink_20),
		.scan_in_20 (pico_scanlink_20),
		.scan_out_20 (pico_scanlink_21),
		.scan_in_21 (pico_scanlink_21),
		.scan_out_21 (pico_scanlink_22),
		.scan_in_22 (pico_scanlink_22),
		.scan_out_22 (pico_scanlink_23),
		.scan_in_23 (pico_scanlink_23),
		.scan_out_23 (pico_scanlink_24),
		.scan_in_24 (pico_scanlink_24),
		.scan_out_24 (pico_scanlink_25),
		.scan_in_25 (pico_scanlink_25),
		.scan_out_25 (pico_scanlink_26),
		.scan_in_26 (pico_scanlink_26),
		.scan_out_26 (pico_scanlink_27),
		.scan_in_27 (pico_scanlink_27),
		.scan_out_27 (pico_scanlink_28),
		.scan_in_28 (pico_scanlink_28),
		.scan_out_28 (pico_scanlink_29),
		.scan_in_29 (pico_scanlink_29),
		.scan_out_29 (pico_scanlink_30),
		.scan_in_30 (pico_scanlink_30),
		.scan_out_30 (pico_scanlink_31),
		.scan_in_31 (pico_scanlink_31),
		.scan_out_31 (pico_scanlink_32),
		.scan_in_32 (pico_scanlink_32),
		.scan_out_32 (pico_scanlink_33),
		.scan_in_33 (pico_scanlink_33),
		.scan_out_33 (pico_scanlink_34),
		.scan_in_34 (pico_scanlink_34),
		.scan_out_34 (pico_scanlink_35),
		.scan_in_35 (pico_scanlink_35),
		.scan_out_35 (pico_scanlink_36),
		.scan_in_36 (pico_scanlink_36),
		.scan_out_36 (pico_scanlink_37),
		.scan_in_37 (pico_scanlink_37),
		.scan_out_37 (pico_scanlink_38),
		.scan_in_38 (pico_scanlink_38),
		.scan_out_38 (pico_scanlink_39),
		.scan_in_39 (pico_scanlink_39),
		.scan_out_39 (pico_scanlink_40),
		.scan_in_40 (pico_scanlink_40),
		.scan_out_40 (pico_scanlink_41),
		.scan_in_41 (pico_scanlink_41),
		.scan_out_41 (pico_scanlink_42),
		.scan_in_42 (pico_scanlink_42),
		.scan_out_42 (pico_scanlink_43),
		.scan_in_43 (pico_scanlink_43),
		.scan_out_43 (pico_scanlink_44),
		.scan_in_44 (pico_scanlink_44),
		.scan_out_44 (pico_scanlink_45),
		.scan_in_45 (pico_scanlink_45),
		.scan_out_45 (pico_scanlink_46),
		.scan_in_46 (pico_scanlink_46),
		.scan_out_46 (pico_scanlink_47),
		.scan_in_47 (pico_scanlink_47),
		.scan_out_47 (pico_scanlink_48),
		.scan_in_48 (pico_scanlink_48),
		.scan_out_48 (pico_scanlink_49),
		.scan_in_49 (pico_scanlink_49),
		.scan_out_49 (pico_scanlink_50),
		.scan_in_50 (pico_scanlink_50),
		.scan_out_50 (pico_scanlink_51),
		.scan_in_51 (pico_scanlink_51),
		.scan_out_51 (pico_scanlink_52),
		.scan_in_52 (pico_scanlink_52),
		.scan_out_52 (pico_scanlink_53),
		.scan_in_53 (pico_scanlink_53),
		.scan_out_53 (pico_scanlink_54),
		.scan_in_54 (pico_scanlink_54),
		.scan_out_54 (pico_scanlink_55),
		.scan_in_55 (pico_scanlink_55),
		.scan_out_55 (pico_scanlink_56),
		.scan_in_56 (pico_scanlink_56),
		.scan_out_56 (pico_scanlink_57),
		.scan_in_57 (pico_scanlink_57),
		.scan_out_57 (pico_scanlink_58),
		.scan_in_58 (pico_scanlink_58),
		.scan_out_58 (pico_scanlink_59),
		.scan_in_59 (pico_scanlink_59),
		.scan_out_59 (pico_scanlink_60),
		.scan_in_60 (pico_scanlink_60),
		.scan_out_60 (pico_scanlink_61),
		.scan_in_61 (pico_scanlink_61),
		.scan_out_61 (pico_scanlink_62),
		.scan_in_62 (pico_scanlink_62),
		.scan_out_62 (pico_scanlink_63),
		.scan_in_63 (pico_scanlink_63),
		.scan_out_63 (pico_scanlink_64),
		.scan_in_64 (pico_scanlink_64),
		.scan_out_64 (pico_scanlink_65),
		.scan_in_65 (pico_scanlink_65),
		.scan_out_65 (pico_scanlink_66),
		.scan_in_66 (pico_scanlink_66),
		.scan_out_66 (pico_scanlink_67),
		.scan_in_67 (pico_scanlink_67),
		.scan_out_67 (pico_scanlink_68),
		.scan_in_68 (pico_scanlink_68),
		.scan_out_68 (pico_scanlink_69),
		.scan_in_69 (pico_scanlink_69),
		.scan_out_69 (pico_scanlink_70),
		.scan_in_70 (pico_scanlink_70),
		.scan_out_70 (pico_scanlink_71),
		.scan_in_71 (pico_scanlink_71),
		.scan_out_71 (pico_scanlink_72),
		.scan_in_72 (pico_scanlink_72),
		.scan_out_72 (pico_scanlink_73),
		.scan_in_73 (pico_scanlink_73),
		.scan_out_73 (pico_scanlink_74),
		.scan_in_74 (pico_scanlink_74),
		.scan_out_74 (pico_scanlink_75),
		.scan_in_75 (pico_scanlink_75),
		.scan_out_75 (pico_scanlink_76),
		.scan_in_76 (pico_scanlink_76),
		.scan_out_76 (pico_scanlink_77),
		.scan_in_77 (pico_scanlink_77),
		.scan_out_77 (pico_scanlink_78),
		.scan_in_78 (pico_scanlink_78),
		.scan_out_78 (pico_scanlink_79),
		.scan_in_79 (pico_scanlink_79),
		.scan_out_79 (pico_scanlink_80),
		.scan_in_80 (pico_scanlink_80),
		.scan_out_80 (pico_scanlink_81),
		.scan_in_81 (pico_scanlink_81),
		.scan_out_81 (pico_scanlink_82),
		.scan_in_82 (pico_scanlink_82),
		.scan_out_82 (pico_scanlink_83),
		.scan_in_83 (pico_scanlink_83),
		.scan_out_83 (pico_scanlink_84),
		.scan_in_84 (pico_scanlink_84),
		.scan_out_84 (pico_scanlink_85),
		.scan_in_85 (pico_scanlink_85),
		.scan_out_85 (pico_scanlink_86),
		.scan_in_86 (pico_scanlink_86),
		.scan_out_86 (pico_scanlink_87),
		.scan_in_87 (pico_scanlink_87),
		.scan_out_87 (pico_scanlink_88),
		.scan_in_88 (pico_scanlink_88),
		.scan_out_88 (pico_scanlink_89),
		.scan_in_89 (pico_scanlink_89),
		.scan_out_89 (pico_scanlink_90),
		.scan_in_90 (pico_scanlink_90),
		.scan_out_90 (pico_scanlink_91),
		.scan_in_91 (pico_scanlink_91),
		.scan_out_91 (pico_scanlink_92),
		.scan_in_92 (pico_scanlink_92),
		.scan_out_92 (pico_scanlink_93),
		.scan_in_93 (pico_scanlink_93),
		.scan_out_93 (pico_scanlink_94),
		.scan_in_94 (pico_scanlink_94),
		.scan_out_94 (pico_scanlink_95),
		.scan_in_95 (pico_scanlink_95),
		.scan_out_95 (pico_scanlink_96),
		.scan_in_96 (pico_scanlink_96),
		.scan_out_96 (pico_scanlink_97),
		.scan_in_97 (pico_scanlink_97),
		.scan_out_97 (pico_scanlink_98),
		.scan_in_98 (pico_scanlink_98),
		.scan_out_98 (pico_scanlink_99),
		.scan_in_99 (pico_scanlink_99),
		.scan_out_99 (pico_scanlink_100),
		.scan_in_100 (pico_scanlink_100),
		.scan_out_100 (pico_scanlink_101),
		.scan_in_101 (pico_scanlink_101),
		.scan_out_101 (pico_scanlink_102),
		.scan_in_102 (pico_scanlink_102),
		.scan_out_102 (pico_scanlink_103),
		.scan_in_103 (pico_scanlink_103),
		.scan_out_103 (pico_scanlink_104),
		.scan_in_104 (pico_scanlink_104),
		.scan_out_104 (pico_scanlink_105),
		.scan_in_105 (pico_scanlink_105),
		.scan_out_105 (pico_scanlink_106),
		.scan_in_106 (pico_scanlink_106),
		.scan_out_106 (pico_scanlink_107),
		.scan_in_107 (pico_scanlink_107),
		.scan_out_107 (pico_scanlink_108),
		.scan_in_108 (pico_scanlink_108),
		.scan_out_108 (pico_scanlink_109),
		.scan_in_109 (pico_scanlink_109),
		.scan_out_109 (pico_scanlink_110),
		.scan_in_110 (pico_scanlink_110),
		.scan_out_110 (pico_scanlink_111),
		.scan_in_111 (pico_scanlink_111),
		.scan_out_111 (pico_scanlink_112),
		.scan_in_112 (pico_scanlink_112),
		.scan_out_112 (pico_scanlink_113),
		.scan_in_113 (pico_scanlink_113),
		.scan_out_113 (pico_scanlink_114),
		.scan_in_114 (pico_scanlink_114),
		.scan_out_114 (pico_scanlink_115),
		.scan_in_115 (pico_scanlink_115),
		.scan_out_115 (pico_scanlink_116),
		.scan_in_116 (pico_scanlink_116),
		.scan_out_116 (pico_scanlink_117),
		.scan_in_117 (pico_scanlink_117),
		.scan_out_117 (pico_scanlink_118),
		.scan_in_118 (pico_scanlink_118),
		.scan_out_118 (pico_scanlink_119),
		.scan_in_119 (pico_scanlink_119),
		.scan_out_119 (pico_scanlink_120),
		.scan_in_120 (pico_scanlink_120),
		.scan_out_120 (pico_scanlink_121),
		.scan_in_121 (pico_scanlink_121),
		.scan_out_121 (pico_scanlink_122),
		.scan_in_122 (pico_scanlink_122),
		.scan_out_122 (pico_scanlink_123),
		.scan_in_123 (pico_scanlink_123),
		.scan_out_123 (pico_scanlink_124),
		.scan_in_124 (pico_scanlink_124),
		.scan_out_124 (pico_scanlink_125),
		.scan_in_125 (pico_scanlink_125),
		.scan_out_125 (pico_scanlink_126),
		.scan_in_126 (pico_scanlink_126),
		.scan_out_126 (pico_scanlink_127),
		.scan_in_127 (pico_scanlink_127),
		.scan_out_127 (pico_scanlink_128),
		.scan_in_128 (pico_scanlink_128),
		.scan_out_128 (pico_scanlink_129),
		.scan_in_129 (pico_scanlink_129),
		.scan_out_129 (pico_scanlink_130),
		.scan_in_130 (pico_scanlink_130),
		.scan_out_130 (pico_scanlink_131),
		.scan_in_131 (pico_scanlink_131),
		.scan_out_131 (pico_scanlink_132),
		.scan_in_132 (pico_scanlink_132),
		.scan_out_132 (pico_scanlink_133),
		.scan_in_133 (pico_scanlink_133),
		.scan_out_133 (pico_scanlink_134),
		.scan_in_134 (pico_scanlink_134),
		.scan_out_134 (pico_scanlink_135),
		.scan_in_135 (pico_scanlink_135),
		.scan_out_135 (pico_scanlink_136),
		.scan_in_136 (pico_scanlink_136),
		.scan_out_136 (pico_scanlink_137),
		.scan_in_137 (pico_scanlink_137),
		.scan_out_137 (pico_scanlink_138),
		.scan_in_138 (pico_scanlink_138),
		.scan_out_138 (pico_scanlink_139),
		.scan_in_139 (pico_scanlink_139),
		.scan_out_139 (pico_scanlink_140),
		.scan_in_140 (pico_scanlink_140),
		.scan_out_140 (pico_scanlink_141),
		.scan_in_141 (pico_scanlink_141),
		.scan_out_141 (pico_scanlink_142),
		.scan_in_142 (pico_scanlink_142),
		.scan_out_142 (pico_scanlink_143),
		.scan_in_143 (pico_scanlink_143),
		.scan_out_143 (pico_scanlink_144),
		.scan_in_144 (pico_scanlink_144),
		.scan_out_144 (pico_scanlink_145),
		.scan_in_145 (pico_scanlink_145),
		.scan_out_145 (pico_scanlink_146),
		.scan_in_146 (pico_scanlink_146),
		.scan_out_146 (pico_scanlink_147),
		.scan_in_147 (pico_scanlink_147),
		.scan_out_147 (pico_scanlink_148),
		.scan_in_148 (pico_scanlink_148),
		.scan_out_148 (pico_scanlink_149),
		.scan_in_149 (pico_scanlink_149),
		.scan_out_149 (pico_scanlink_150),
		.scan_in_150 (pico_scanlink_150),
		.scan_out_150 (pico_scanlink_151),
		.scan_in_151 (pico_scanlink_151),
		.scan_out_151 (pico_scanlink_152),
		.scan_in_152 (pico_scanlink_152),
		.scan_out_152 (pico_scanlink_153),
		.scan_in_153 (pico_scanlink_153),
		.scan_out_153 (pico_scanlink_154),
		.scan_in_154 (pico_scanlink_154),
		.scan_out_154 (pico_scanlink_155),
		.scan_in_155 (pico_scanlink_155),
		.scan_out_155 (pico_scanlink_156),
		.scan_in_156 (pico_scanlink_156),
		.scan_out_156 (pico_scanlink_157),
		.scan_in_157 (pico_scanlink_157),
		.scan_out_157 (pico_scanlink_158),
		.scan_in_158 (pico_scanlink_158),
		.scan_out_158 (pico_scanlink_159),
		.scan_in_159 (pico_scanlink_159),
		.scan_out_159 (pico_scanlink_160),
		.scan_in_160 (pico_scanlink_160),
		.scan_out_160 (pico_scanlink_161),
		.scan_in_161 (pico_scanlink_161),
		.scan_out_161 (picorv32a_scan_out),
		.wbr_se (soc_wbr_se),
		.wbr_si (soc_wbr_si),
		.wbr_so (wbr_mid)
	);


	wire uart_scan_en_tie = soc_wbr_se;
	wire uart_scanlink_1;

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
		.ser_rx (ser_rx),
		.ser_tx (ser_tx),
		.scan_en (uart_scan_en_tie),
		.scan_in_0 (uart_scan_in),
		.scan_out_0 (uart_scanlink_1),
		.scan_in_1 (uart_scanlink_1),
		.scan_out_1 (uart_scan_out),
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

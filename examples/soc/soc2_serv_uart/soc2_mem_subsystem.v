// soc2_mem_subsystem.v -- SoC2's memory subsystem (data/instruction RAM +
// SERV register-file storage), extracted as its own wrapped block for the
// same reason as SoC1's soc1_ram: assemble_soc's graybox EXTEST mode only
// drops *block* core logic, so bare RAM/RF arrays left directly in the glue
// survive as real, unfused flip-flops and trip the native ATPG engine's
// combinational-only check. Wraps servant_ram.v + serv_rf_ram.v unmodified.
`default_nettype none

module soc2_mem_subsystem #(
	parameter integer MEMSIZE        = 8192,
	parameter         RESET_STRATEGY = "MINI",
	parameter         RF_WIDTH       = 2,
	parameter         RF_L2D         = 10,
	parameter         CSR_REGS       = 4
) (
	input  wire        clk,
	input  wire        rst,

	input  wire [31:0] wb_mem_adr,
	input  wire [31:0] wb_mem_dat,
	input  wire [ 3:0] wb_mem_sel,
	input  wire        wb_mem_we,
	input  wire        wb_mem_stb,
	output wire [31:0] wb_mem_rdt,
	output wire        wb_mem_ack,

	input  wire [RF_L2D-1:0]   rf_waddr,
	input  wire [RF_WIDTH-1:0] rf_wdata,
	input  wire                rf_wen,
	input  wire [RF_L2D-1:0]   rf_raddr,
	input  wire                rf_ren,
	output wire [RF_WIDTH-1:0] rf_rdata
);

	servant_ram #(
		.depth          (MEMSIZE),
		.RESET_STRATEGY (RESET_STRATEGY),
		.memfile        ("")
	) ram (
		.i_wb_clk (clk),
		.i_wb_rst (rst),
		.i_wb_adr (wb_mem_adr[$clog2(MEMSIZE)-1:2]),
		.i_wb_cyc (wb_mem_stb),
		.i_wb_we  (wb_mem_we),
		.i_wb_sel (wb_mem_sel),
		.i_wb_dat (wb_mem_dat),
		.o_wb_rdt (wb_mem_rdt),
		.o_wb_ack (wb_mem_ack)
	);

	serv_rf_ram #(
		.width    (RF_WIDTH),
		.csr_regs (CSR_REGS)
	) rf_ram (
		.i_clk   (clk),
		.i_waddr (rf_waddr),
		.i_wdata (rf_wdata),
		.i_wen   (rf_wen),
		.i_raddr (rf_raddr),
		.i_ren   (rf_ren),
		.o_rdata (rf_rdata)
	);

endmodule

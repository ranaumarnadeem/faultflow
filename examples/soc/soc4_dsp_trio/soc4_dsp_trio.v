// soc4_dsp_trio.v -- three-stage DSP filter pipeline SoC composed entirely
// from existing benchmark blocks (no new IP sourced): a raw sample is
// smoothed by an IIR averager, then a boxcar averager, then shaped by an
// 8-tap adjustable FIR. Each stage's RTL is unmodified, from
// examples/dspfilters/rtl/{iiravg,boxcar}.v and
// examples/dsp_genericfir_small.v.
`default_nettype none

module soc4_dsp_trio (
	input  wire        i_clk,
	input  wire        i_reset,
	input  wire        i_ce,

	input  wire [14:0] i_sample,

	input  wire [ 5:0] i_navg,

	input  wire        i_tap_wr,
	input  wire [ 7:0] i_tap,

	output wire [22:0] o_result
);

	wire [15:0] iir_out;
	wire [21:0] boxcar_out;

	iiravg #(
		.IW (15),
		.OW (16)
	) u_iiravg (
		.i_clk   (i_clk),
		.i_reset (i_reset),
		.i_ce    (i_ce),
		.i_data  (i_sample),
		.o_data  (iir_out)
	);

	boxcar #(
		.IW (16),
		.LGMEM (6)
	) u_boxcar (
		.i_clk    (i_clk),
		.i_reset  (i_reset),
		.i_navg   (i_navg),
		.i_ce     (i_ce),
		.i_sample (iir_out),
		.o_result (boxcar_out)
	);

	genericfir_small u_genericfir_small (
		.i_clk    (i_clk),
		.i_reset  (i_reset),
		.i_tap_wr (i_tap_wr),
		.i_tap    (i_tap),
		.i_ce     (i_ce),
		.i_sample (boxcar_out[21:14]),
		.o_result (o_result)
	);

endmodule
